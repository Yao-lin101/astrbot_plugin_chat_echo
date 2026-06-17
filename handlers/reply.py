from astrbot.api.event import AstrMessageEvent

from ..helpers import maybe_typing_delay
from ..services.image_caption import ensure_context_captions
from ..tracker import ConversationTracker

MAX_CONTEXT_MESSAGES = 20


def build_analyze_context(tracker: ConversationTracker) -> tuple[str, list[str]]:
    """Build the analysis context string chronologically and gather all associated image URLs."""
    lines = ["=== 群聊对话记录 (按时间顺序) ==="]
    idx = 1
    if tracker.trigger_message:
        lines.append(f"{idx}. {tracker.trigger_user_name}: {tracker.trigger_message}")
        idx += 1

    lines.append(f"{idx}. 你: {tracker.bot_message or '[你发送了一条消息]'}")
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


def build_batch_context(
    tracker: ConversationTracker, batch_messages: list[dict]
) -> tuple[str, list[str]]:
    """Build analysis context for batch mode, marking the batch messages clearly."""
    lines = ["=== 群聊对话记录 (批次分析, 按时间顺序) ==="]
    idx = 1
    if tracker.trigger_message:
        lines.append(f"{idx}. {tracker.trigger_user_name}: {tracker.trigger_message}")
        idx += 1

    lines.append(f"{idx}. 你: {tracker.bot_message or '[你发送了一条消息]'}")
    idx += 1

    all_image_urls = []

    # Mark batch messages explicitly
    lines.append("[以下为本批次积累的消息，请综合判断是否在回复你:]")
    batch = batch_messages
    if len(batch) > MAX_CONTEXT_MESSAGES:
        batch = batch[-MAX_CONTEXT_MESSAGES:]
        lines.append(
            f"[仅显示最近 {MAX_CONTEXT_MESSAGES} 条消息, 本批次共 {len(batch_messages)} 条]"
        )
    for msg in batch:
        lines.append(f"{idx}. {msg['user_name']}: {msg['content']}")
        idx += 1
        if msg.get("image_urls"):
            all_image_urls.extend(msg["image_urls"])
    return "\n".join(lines), all_image_urls


async def handle_reply(
    plugin, tracker: ConversationTracker, event: AstrMessageEvent
) -> bool:
    """Process message under active tracking window (Route 1).
    Returns True if reply is triggered, False otherwise.
    """
    group_id = tracker.group_id
    try:
        await ensure_context_captions(
            plugin, tracker.collected, tracker.unified_msg_origin
        )
        context_text, image_urls = build_analyze_context(tracker)
        if plugin.config_helper.enable_image_caption():
            image_urls = None

        persona_name = ""
        try:
            personality = await plugin.context.persona_manager.get_default_persona_v3(
                tracker.unified_msg_origin
            )
            if personality:
                persona_name = personality.get("name") or ""
        except Exception:
            pass

        plugin.logger.info(
            f"[Reply] Analyzing if response is targeted to Bot in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_analyzer(
            context_text,
            image_urls=image_urls,
            umo=tracker.unified_msg_origin,
            self_id=event.get_self_id(),
            persona_name=persona_name,
        )
        if analysis is None:
            return False
        is_reply = analysis.get("need_reply", "no")
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
            return False

        plugin.logger.info(
            f"[Reply] Group {group_id} is replying to Bot | Reason: {reason}"
        )
        tracker.detection_count = 0
        await maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(f"[Reply] Error in handle_reply: {e}")
        return False
    finally:
        tracker.analyzing = False


async def handle_reply_batch(
    plugin,
    tracker: ConversationTracker,
    event: AstrMessageEvent,
    batch_messages: list[dict],
) -> bool:
    """Batch version of handle_reply: analyze accumulated batch messages at once.
    Returns True if reply is triggered, False otherwise.
    """
    group_id = tracker.group_id
    try:
        await ensure_context_captions(
            plugin, batch_messages, tracker.unified_msg_origin
        )
        context_text, image_urls = build_batch_context(tracker, batch_messages)
        if plugin.config_helper.enable_image_caption():
            image_urls = None

        persona_name = ""
        try:
            personality = await plugin.context.persona_manager.get_default_persona_v3(
                tracker.unified_msg_origin
            )
            if personality:
                persona_name = personality.get("name") or ""
        except Exception:
            pass

        plugin.logger.info(
            f"[ReplyBatch] Analyzing batch ({len(batch_messages)} msgs) in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_analyzer(
            context_text,
            image_urls=image_urls,
            umo=tracker.unified_msg_origin,
            self_id=event.get_self_id(),
            persona_name=persona_name,
        )
        if analysis is None:
            return False
        is_reply = analysis.get("need_reply", "no")
        reason = analysis.get("reason", "")

        # Each batch counts as detection_count increment + number of messages in batch
        # We add the batch size as detection weight
        tracker.detection_count += len(batch_messages)
        max_detect = plugin.config_helper.max_detection_count()

        if is_reply == "no":
            plugin.logger.info(
                f"[ReplyBatch] Group {group_id} batch not replying to Bot ({reason}) | "
                f"{tracker.detection_count}/{max_detect}"
            )
            if tracker.detection_count >= max_detect:
                plugin.logger.info(
                    f"[ReplyBatch] Max detection count reached for group {group_id}, stopping track."
                )
                plugin.tracker_manager.cleanup_tracker(group_id)
            return False

        plugin.logger.info(
            f"[ReplyBatch] Group {group_id} batch is replying to Bot | Reason: {reason}"
        )
        tracker.detection_count = 0
        await maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(f"[ReplyBatch] Error in handle_reply_batch: {e}")
        return False
    finally:
        tracker.analyzing = False
