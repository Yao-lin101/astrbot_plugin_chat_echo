import asyncio
import random
import time

from astrbot.api.event import AstrMessageEvent

from .helpers import extract_image_urls
from .tracker import ConversationTracker

MAX_CONTEXT_MESSAGES = 20


async def _maybe_typing_delay(plugin) -> None:
    """Apply random typing delay if human_like_mode is enabled."""
    if plugin.config_helper.human_like_mode():
        d_min = plugin.config_helper.typing_delay_min()
        d_max = plugin.config_helper.typing_delay_max()
        if d_max > 0:
            await asyncio.sleep(random.uniform(d_min, d_max))


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


def build_proactive_batch_context(batch_messages: list[dict]) -> tuple[str, list[str]]:
    """Build proactive analysis context for batch mode."""
    context_lines = ["=== 群聊中的最近消息 (批次分析) ==="]
    context_lines.append("[以下为本批次积累的消息，请综合判断是否应该参与讨论:]")
    all_image_urls = []
    for m in batch_messages:
        context_lines.append(f"{m['user_name']}: {m['content']}")
        if m.get("image_urls"):
            all_image_urls.extend(m["image_urls"])
    context_text = "\n".join(context_lines)
    return context_text, all_image_urls


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
            caption = await plugin.get_image_caption(
                url, event.unified_msg_origin, force=True
            )
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


async def ensure_context_captions(
    plugin, messages: list[dict], umo: str
) -> list[asyncio.Task]:
    """Lazily caption any uncaptioned images in the message list in-place.
    Returns list of background caption tasks (fire-and-forget).
    """
    tasks = []
    if not plugin.config_helper.enable_image_caption():
        return tasks
    for msg in messages:
        image_urls = msg.get("image_urls")
        if image_urls and "[图片描述:" not in msg.get("content", ""):
            captions = []
            for url in image_urls:
                caption = await plugin.get_image_caption(url, umo, force=True)
                if caption:
                    captions.append(caption)
            if captions:
                msg["content"] += " " + " ".join(
                    f"[图片描述: {cap}]" for cap in captions
                )
    return tasks


async def prewarm_captions(plugin, msg: dict, umo: str) -> list[asyncio.Task]:
    """Start background image caption tasks for a single message.
    Returns list of tasks that will save captions into the cache.
    """
    tasks = []
    if not plugin.config_helper.enable_image_caption():
        return tasks
    image_urls = msg.get("image_urls", [])
    if not image_urls:
        return tasks
    for url in image_urls:
        # Fire-and-forget: start captioning in background, result goes to cache
        task = asyncio.create_task(plugin.get_image_caption(url, umo, force=True))
        tasks.append(task)
    return tasks


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
            return False

        plugin.logger.info(
            f"[Reply] Group {group_id} is replying to Bot | Reason: {reason}"
        )
        tracker.detection_count = 0
        await _maybe_typing_delay(plugin)
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
        is_reply = analysis.get("is_reply_to_bot", "no")
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
        await _maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(f"[ReplyBatch] Error in handle_reply_batch: {e}")
        return False
    finally:
        tracker.analyzing = False


async def handle_proactive(
    plugin, event: AstrMessageEvent, msg: dict, recent_window: list[dict]
) -> bool:
    """Process message under proactive activation check (Route 2).
    Returns True if proactive participation is approved, False otherwise.
    """
    group_id = str(event.get_group_id())
    try:
        await ensure_context_captions(plugin, recent_window, event.unified_msg_origin)
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

        persona_name = ""
        try:
            personality = await plugin.context.persona_manager.get_default_persona_v3(
                event.unified_msg_origin
            )
            if personality:
                persona_name = personality.get("name") or ""
        except Exception:
            pass

        plugin.logger.info(
            f"[Proactive] Analyzing if Bot should participate in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_proactive_analyzer(
            context_text,
            image_urls=all_image_urls,
            umo=event.unified_msg_origin,
            self_id=event.get_self_id(),
            persona_name=persona_name,
        )
        if analysis is None:
            return False
        should_join = analysis.get("should_join", "no")
        reason = analysis.get("reason", "")
        if should_join == "no":
            plugin.logger.info(
                f"[Proactive] Group {group_id} does not warrant participation ({reason})"
            )
            return False
        plugin.logger.info(
            f"[Proactive] Group {group_id} approved for participation | Reason: {reason}"
        )

        rounds = plugin.tracker_manager.increment_proactive_rounds(group_id)
        max_rounds = plugin.config_helper.max_rounds()
        plugin.logger.info(
            f"[Proactive] Speaking to group {group_id} natively (Round {rounds}/{max_rounds})"
        )

        plugin.tracker_manager.set_active_cooldown(group_id, time.time())
        if rounds >= max_rounds:
            plugin.logger.info(
                f"[Proactive] Group {group_id} reached max rounds limit."
            )
        await _maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(f"[Proactive] Error in handle_proactive: {e}")
        plugin.tracker_manager.set_active_thinking(group_id, False)
        return False


async def handle_proactive_batch(
    plugin,
    event: AstrMessageEvent,
    batch_messages: list[dict],
) -> bool:
    """Batch version of handle_proactive: analyze accumulated batch messages at once."""
    group_id = str(event.get_group_id())
    try:
        await ensure_context_captions(plugin, batch_messages, event.unified_msg_origin)
        gname = ""
        try:
            g = await event.get_group()
            gname = g.group_name if g else ""
            if gname:
                plugin.token_counter.set_group_name(group_id, gname)
        except Exception as e:
            plugin.logger.exception(f"[ProactiveBatch] Failed to get group name: {e}")

        context_text, all_image_urls = build_proactive_batch_context(batch_messages)
        if plugin.config_helper.enable_image_caption():
            all_image_urls = None

        persona_name = ""
        try:
            personality = await plugin.context.persona_manager.get_default_persona_v3(
                event.unified_msg_origin
            )
            if personality:
                persona_name = personality.get("name") or ""
        except Exception:
            pass

        plugin.logger.info(
            f"[ProactiveBatch] Analyzing batch ({len(batch_messages)} msgs) in group {group_id}..."
        )
        analysis = await plugin.llm_handler.call_proactive_analyzer(
            context_text,
            image_urls=all_image_urls,
            umo=event.unified_msg_origin,
            self_id=event.get_self_id(),
            persona_name=persona_name,
        )
        if analysis is None:
            return False
        should_join = analysis.get("should_join", "no")
        reason = analysis.get("reason", "")
        if should_join == "no":
            plugin.logger.info(
                f"[ProactiveBatch] Group {group_id} does not warrant participation ({reason})"
            )
            return False
        plugin.logger.info(
            f"[ProactiveBatch] Group {group_id} approved for participation | Reason: {reason}"
        )

        rounds = plugin.tracker_manager.increment_proactive_rounds(group_id)
        max_rounds = plugin.config_helper.max_rounds()
        plugin.logger.info(
            f"[ProactiveBatch] Speaking to group {group_id} natively (Round {rounds}/{max_rounds})"
        )

        plugin.tracker_manager.set_active_cooldown(group_id, time.time())
        if rounds >= max_rounds:
            plugin.logger.info(
                f"[ProactiveBatch] Group {group_id} reached max rounds limit."
            )
        await _maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(
            f"[ProactiveBatch] Error in handle_proactive_batch: {e}"
        )
        return False
    finally:
        plugin.tracker_manager.set_active_thinking(group_id, False)


async def handle_keyword(
    plugin,
    event: AstrMessageEvent,
    msg: dict,
    recent_window: list[dict],
    matched_keyword: str,
) -> bool:
    """Process message under keyword trigger (Route 3).
    Returns True if reply is triggered, False otherwise.
    """
    group_id = str(event.get_group_id())
    try:
        plugin.logger.info(
            f"[Keyword] Keyword '{matched_keyword}' matched in group {group_id}. Triggering native reply..."
        )
        await _maybe_typing_delay(plugin)
        return True
    except Exception as e:
        plugin.logger.exception(f"[Keyword] Error in handle_keyword: {e}")
        return False
