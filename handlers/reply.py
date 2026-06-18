from astrbot.api.event import AstrMessageEvent

from ..helpers import maybe_typing_delay
from ..services.image_caption import ensure_context_captions
from ..tracker import ConversationTracker

MAX_CONTEXT_MESSAGES = 10


def build_analyze_context(plugin, tracker: ConversationTracker) -> tuple[str, list[str]]:
    """Build the analysis context string chronologically using native GroupChatContext records."""
    lines = ["=== 群聊对话记录 (按时间顺序) ==="]
    idx = 1
    
    gcc = plugin.get_group_chat_context()
    if gcc:
        records = list(gcc.raw_records.get(tracker.unified_msg_origin, []))
        relevant_records = records[-MAX_CONTEXT_MESSAGES:] if records else []
        for record in relevant_records:
            lines.append(f"{idx}. {record}")
            idx += 1
            
    return "\n".join(lines), None


def build_batch_context(
    plugin, tracker: ConversationTracker, batch_messages: list[dict]
) -> tuple[str, list[str]]:
    """Build analysis context for batch mode using native GroupChatContext."""
    lines = ["=== 群聊对话记录 (批次分析, 按时间顺序) ==="]
    idx = 1
    
    gcc = plugin.get_group_chat_context()
    if gcc:
        records = list(gcc.raw_records.get(tracker.unified_msg_origin, []))
        relevant_records = records[-MAX_CONTEXT_MESSAGES:] if records else []
        for record in relevant_records:
            lines.append(f"{idx}. {record}")
            idx += 1
            
    return "\n".join(lines), None


async def handle_reply(
    plugin, tracker: ConversationTracker, event: AstrMessageEvent
) -> bool:
    """Process message under active tracking window (Route 1).
    Returns True if reply is triggered, False otherwise.
    """
    group_id = tracker.group_id
    try:
        context_text, image_urls = build_analyze_context(plugin, tracker)

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
            plugin.logger.warning(
                f"[Reply] Group {group_id} reply analysis failed (LLM returned None or invalid JSON)."
            )
            return False
        need_reply = analysis.get("need_reply", False)
        if isinstance(need_reply, str):
            need_reply = need_reply.strip().lower() in ("true", "yes")
        reason = analysis.get("reason", "")
        if not need_reply:
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
        context_text, image_urls = build_batch_context(plugin, tracker, batch_messages)

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
            plugin.logger.warning(
                f"[ReplyBatch] Group {group_id} batch reply analysis failed (LLM returned None or invalid JSON)."
            )
            return False
        need_reply = analysis.get("need_reply", False)
        if isinstance(need_reply, str):
            need_reply = need_reply.strip().lower() in ("true", "yes")
        reason = analysis.get("reason", "")

        # Each batch counts as detection_count increment + number of messages in batch
        # We add the batch size as detection weight
        tracker.detection_count += len(batch_messages)
        max_detect = plugin.config_helper.max_detection_count()

        if not need_reply:
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
