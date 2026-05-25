import datetime
import time
import zoneinfo

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.star_handler import EventType

from .helpers import extract_image_urls
from .tracker import ConversationTracker

MAX_CONTEXT_MESSAGES = 20


def build_system_reminder(event: AstrMessageEvent, global_cfg: dict) -> str:
    """Build a <system_reminder> block matching the formatting of astr_main_agent."""
    provider_settings = global_cfg.get("provider_settings", {})
    timezone = global_cfg.get("timezone")

    system_parts = []
    if provider_settings.get("identifier"):
        try:
            user_id = event.message_obj.sender.user_id
            user_nickname = event.message_obj.sender.nickname
            if user_id:
                system_parts.append(
                    f"User ID: {user_id}, Nickname: {user_nickname or ''}"
                )
        except Exception:
            pass

    if provider_settings.get("group_name_display") and event.message_obj.group_id:
        try:
            if event.message_obj.group and event.message_obj.group.group_name:
                system_parts.append(f"Group name: {event.message_obj.group.group_name}")
        except Exception:
            pass

    if provider_settings.get("datetime_system_prompt"):
        current_time = None
        if timezone:
            try:
                now = datetime.datetime.now(zoneinfo.ZoneInfo(timezone))
                current_time = now.strftime("%Y-%m-%d %H:%M (%Z)")
            except Exception:
                pass
        if not current_time:
            try:
                current_time = (
                    datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M (%Z)")
                )
            except Exception:
                pass
        if current_time:
            system_parts.append(f"Current datetime: {current_time}")

    if system_parts:
        return "<system_reminder>" + "\n".join(system_parts) + "</system_reminder>"
    return ""


def build_analyze_context(tracker: ConversationTracker) -> tuple[str, list[str]]:
    """Build the analysis context string chronologically and gather all associated image URLs."""
    lines = ["=== 群聊对话记录 (按时间顺序) ==="]
    idx = 1
    if tracker.trigger_message:
        lines.append(f"{idx}. {tracker.trigger_user_name}: {tracker.trigger_message}")
        idx += 1

    lines.append(f"{idx}. Bot: {tracker.bot_message or '[Bot发送了一条消息]'}")
    idx += 1

    collected = tracker.collected
    all_image_urls = []
    if len(collected) > MAX_CONTEXT_MESSAGES:
        collected = collected[-MAX_CONTEXT_MESSAGES:]
        lines.append(
            f"[仅显示最近 {MAX_CONTEXT_MESSAGES} 条消息, 共 {len(tracker.collected)} 条]"
        )
    for msg in collected:
        lines.append(f"{idx}. {msg['user_name']}: {msg['content']}")
        idx += 1
        if msg.get("image_urls"):
            all_image_urls.extend(msg["image_urls"])
    return "\n".join(lines), all_image_urls


async def start_tracking(
    plugin, event: AstrMessageEvent, bot_message: str = ""
) -> None:
    """Initialize tracking for a group chat response context."""
    group_id = str(event.get_group_id())
    if plugin.tracker_manager.has_active_tracker(group_id):
        return
    gname = ""
    try:
        g = await event.get_group()
        gname = g.group_name if g else ""
    except Exception as e:
        plugin.logger.exception(f"Failed to get group name: {e}")

    trigger_message = event.message_str or ""
    if not trigger_message.strip():
        trigger_message = event.get_message_outline()

    image_urls = await extract_image_urls(event)
    if image_urls and plugin.config_helper.enable_image_caption():
        captions = []
        for url in image_urls:
            caption = await plugin.get_image_caption(url, event.unified_msg_origin)
            if caption:
                captions.append(caption)
        if captions:
            trigger_message += " " + " ".join(f"[图片描述: {cap}]" for cap in captions)

    plugin.tracker_manager.start_tracking(
        group_id=group_id,
        unified_msg_origin=event.unified_msg_origin,
        bot_message=bot_message,
        trigger_user_name=event.get_sender_name(),
        trigger_user_id=str(event.get_sender_id()),
        trigger_message=trigger_message,
        expire_seconds=plugin.config_helper.track_timeout(),
        group_name=gname,
    )
    if gname:
        plugin.token_counter.set_group_name(group_id, gname)


async def handle_reply(
    plugin, tracker: ConversationTracker, event: AstrMessageEvent
) -> MessageEventResult | None:
    """Process message under active tracking window (Route 1)."""
    group_id = tracker.group_id
    try:
        context_text, image_urls = build_analyze_context(tracker)
        if plugin.config_helper.enable_image_caption():
            image_urls = None

        plugin.logger.info(
            f"[Reply] Analyzing if response is targeted to Bot in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_analyzer(
            context_text, image_urls=image_urls, umo=tracker.unified_msg_origin
        )
        if analysis is None:
            return None
        is_reply = analysis.get("is_reply_to_bot", "no")
        reason = analysis.get("reason", "")
        if is_reply == "no":
            tracker.detection_count += 1
            max_detect = plugin.config_helper.max_detection_count()
            plugin.logger.info(
                f"[Reply] Group {group_id} does not reply to Bot ({reason}) | "
                f"{tracker.detection_count}/{max_detect}"
            )
            if tracker.detection_count >= max_detect:
                plugin.logger.info(
                    f"[Reply] Max detection count reached for group {group_id}, stopping track."
                )
                plugin.tracker_manager.cleanup_tracker(group_id)
            return None

        plugin.logger.info(
            f"[Reply] Group {group_id} is replying to Bot | Reason: {reason}"
        )
        if plugin.config_helper.enable_llm_tools():
            reply_text = await plugin.llm_handler.call_generator_with_tools(
                context_text,
                event=event,
                image_urls=image_urls,
                umo=tracker.unified_msg_origin,
            )
        else:
            reply_text = await plugin.llm_handler.call_generator_raw(
                context_text, image_urls=image_urls, umo=tracker.unified_msg_origin
            )
        if not reply_text:
            plugin.logger.warning(
                f"[Reply] Empty reply text generated for group {group_id}"
            )
            return None

        # Trigger OnLLMResponseEvent event for plugin cooperation (e.g. meme_manager)
        llm_response = LLMResponse(role="assistant", completion_text=reply_text)
        await call_event_hook(event, EventType.OnLLMResponseEvent, llm_response)
        reply_text = llm_response.completion_text

        plugin.logger.info(f"[Reply] Replying to group {group_id}: {reply_text[:60]}")
        plugin.tracker_manager.set_proactive_flag(group_id, True)

        result = MessageEventResult()
        result.message(reply_text)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        try:
            conv_mgr = plugin.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(tracker.unified_msg_origin)
            if cid:
                user_msg_content = ""
                if tracker.collected:
                    user_msg_content = tracker.collected[-1].get("content") or ""
                if not user_msg_content:
                    user_msg_content = event.message_str or ""

                global_cfg = plugin.context.get_config(umo=tracker.unified_msg_origin)
                reminder = build_system_reminder(event, global_cfg)

                parts = [TextPart(text=user_msg_content)]
                if reminder:
                    parts.append(TextPart(text=reminder))

                await conv_mgr.add_message_pair(
                    cid=cid,
                    user_message=UserMessageSegment(content=parts),
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=reply_text)]
                    ),
                )
        except Exception as e:
            plugin.logger.exception(
                f"[Reply] Failed to write conversation history: {e}"
            )

        # Append Bot's own response to tracker.collected and update bot_message
        tracker.collected.append(
            {
                "user_name": "Bot",
                "user_id": "bot",
                "content": reply_text,
                "image_urls": [],
                "time": time.time(),
            }
        )
        tracker.bot_message = reply_text
        tracker.detection_count = 0
        tracker.expire_at = time.time() + plugin.config_helper.track_timeout()
        plugin.tracker_manager.set_proactive_flag(group_id, False)
        return result

    except Exception as e:
        plugin.logger.exception(f"[Reply] Error in handle_reply: {e}")
        return None
    finally:
        tracker.analyzing = False
        plugin.tracker_manager.set_proactive_flag(group_id, False)


async def handle_proactive(
    plugin, event: AstrMessageEvent, msg: dict, recent_window: list[dict]
) -> MessageEventResult | None:
    """Process message under proactive activation check (Route 2)."""
    group_id = str(event.get_group_id())
    try:
        gname = ""
        try:
            g = await event.get_group()
            gname = g.group_name if g else ""
            if gname:
                plugin.token_counter.set_group_name(group_id, gname)
        except Exception as e:
            plugin.logger.exception(f"[Proactive] Failed to get group name: {e}")

        context_lines = ["=== 群聊中的最近消息 ==="]
        all_image_urls = []
        for m in recent_window:
            context_lines.append(f"{m['user_name']}: {m['content']}")
            if m.get("image_urls"):
                all_image_urls.extend(m["image_urls"])
        context_text = "\n".join(context_lines)

        if plugin.config_helper.enable_image_caption():
            all_image_urls = None

        plugin.logger.info(
            f"[Proactive] Analyzing if Bot should participate in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_proactive_analyzer(
            context_text, image_urls=all_image_urls, umo=event.unified_msg_origin
        )
        if analysis is None:
            return None
        should_join = analysis.get("should_join", "no")
        reason = analysis.get("reason", "")
        if should_join == "no":
            plugin.logger.info(
                f"[Proactive] Group {group_id} does not warrant participation ({reason})"
            )
            return None
        plugin.logger.info(
            f"[Proactive] Group {group_id} approved for participation | Reason: {reason}"
        )

        reply_text = await plugin.llm_handler.call_generator_raw(
            context_text, image_urls=all_image_urls, umo=event.unified_msg_origin
        )
        if not reply_text:
            return None

        # Trigger OnLLMResponseEvent event for plugin cooperation (e.g. meme_manager)
        llm_response = LLMResponse(role="assistant", completion_text=reply_text)
        await call_event_hook(event, EventType.OnLLMResponseEvent, llm_response)
        reply_text = llm_response.completion_text

        rounds = plugin.tracker_manager.increment_proactive_rounds(group_id)
        max_rounds = plugin.config_helper.max_rounds()
        plugin.logger.info(
            f"[Proactive] Speaking to group {group_id} (Round {rounds}/{max_rounds}): {reply_text[:60]}"
        )
        plugin.tracker_manager.set_proactive_flag(group_id, True)

        result = MessageEventResult()
        result.message(reply_text)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        try:
            conv_mgr = plugin.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                global_cfg = plugin.context.get_config(umo=event.unified_msg_origin)
                reminder = build_system_reminder(event, global_cfg)

                parts = [TextPart(text=msg["content"])]
                if reminder:
                    parts.append(TextPart(text=reminder))

                await conv_mgr.add_message_pair(
                    cid=cid,
                    user_message=UserMessageSegment(content=parts),
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=reply_text)]
                    ),
                )
        except Exception as e:
            plugin.logger.exception(
                f"[Proactive] Failed to write conversation history: {e}"
            )

        # Start tracking group responses to this proactive message
        await start_tracking(plugin, event, reply_text)

        plugin.tracker_manager.set_active_cooldown(group_id, time.time())
        if rounds >= max_rounds:
            plugin.logger.info(
                f"[Proactive] Group {group_id} reached max rounds limit."
            )
        plugin.tracker_manager.set_proactive_flag(group_id, False)
        return result

    except Exception as e:
        plugin.logger.exception(f"[Proactive] Error in handle_proactive: {e}")
        return None
    finally:
        plugin.tracker_manager.set_active_thinking(group_id, False)
        plugin.tracker_manager.set_proactive_flag(group_id, False)
