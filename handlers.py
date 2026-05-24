import time

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

from .tracker import ConversationTracker

MAX_CONTEXT_MESSAGES = 20


def build_analyze_context(tracker: ConversationTracker) -> tuple[str, list[str]]:
    """Build the analysis context string and gather all associated image URLs."""
    lines = [
        "=== Bot 刚才发出的消息 ===",
        tracker.bot_message or "[Bot发送了一条消息]",
        f"\n=== 触发者: {tracker.trigger_user_name} ===",
        f"触发者消息: {tracker.trigger_message or '[未知]'}",
        "\n=== 群聊对话记录 ===",
    ]
    collected = tracker.collected
    all_image_urls = []
    if len(collected) > MAX_CONTEXT_MESSAGES:
        collected = collected[-MAX_CONTEXT_MESSAGES:]
        lines.append(
            f"[仅显示最近 {MAX_CONTEXT_MESSAGES} 条消息, 共 {len(tracker.collected)} 条]"
        )
    for i, msg in enumerate(collected, 1):
        lines.append(f"{i}. {msg['user_name']}: {msg['content']}")
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

    plugin.tracker_manager.start_tracking(
        group_id=group_id,
        unified_msg_origin=event.unified_msg_origin,
        bot_message=bot_message,
        trigger_user_name=event.get_sender_name(),
        trigger_user_id=str(event.get_sender_id()),
        trigger_message=event.message_str,
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

        plugin.logger.info(f"[Reply] Replying to group {group_id}: {reply_text[:60]}")
        plugin.tracker_manager.set_proactive_flag(group_id, True)

        result = MessageEventResult()
        result.message(reply_text)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        try:
            conv_mgr = plugin.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(tracker.unified_msg_origin)
            if cid:
                await conv_mgr.add_message_pair(
                    cid=cid,
                    user_message=UserMessageSegment(
                        content=[TextPart(text=tracker.trigger_message)]
                    ),
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
                await conv_mgr.add_message_pair(
                    cid=cid,
                    user_message=UserMessageSegment(
                        content=[TextPart(text=msg["content"])]
                    ),
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
