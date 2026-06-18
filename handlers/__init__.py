import asyncio
import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Reply

from ..helpers import (
    extract_image_urls,
    is_group_event,
    is_probability_hit,
    maybe_typing_delay,
)
from ..services.image_caption import (
    ensure_context_captions,
    get_image_caption,
    prewarm_captions,
)
from .batch import (
    flush_batch_proactive,
    flush_batch_reply,
    schedule_batch_flush_proactive,
    schedule_batch_flush_reply,
)
from .proactive import handle_proactive
from .reply import handle_reply


async def post_process_group_context(
    plugin, event: AstrMessageEvent, image_urls: list[str]
) -> None:
    """Post-process the latest record in native GroupChatContext to add image captions or Chinese fallback."""
    gcc = plugin.get_group_chat_context()
    if not gcc:
        return
    umo = event.unified_msg_origin
    lock = gcc._get_lock(umo)
    async with lock:
        records = gcc.raw_records.get(umo)
        if not records:
            return

        last_record = records[-1]

        # If the record contains the untranslated native "[Image]" tag
        if " [Image]" in last_record:
            captions = []
            if image_urls:
                for url in image_urls:
                    img_hash = await plugin.caption_cache.get_hash(url)
                    cached = plugin.caption_cache.get(img_hash)
                    if cached:
                        captions.append(cached)
                    elif plugin.config_helper.enable_image_caption():
                        caption = await get_image_caption(
                            plugin, url, umo, force=True
                        )
                        if caption:
                            captions.append(caption)

            if captions:
                new_content = " " + " ".join(
                    f"[图片描述: {cap}]" for cap in captions
                )
                new_record = last_record.replace(" [Image]", new_content)
            else:
                new_record = last_record.replace(" [Image]", " [图片/表情]")

            records[-1] = new_record


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
            caption = await get_image_caption(
                plugin, url, event.unified_msg_origin, force=True
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


async def handle_keyword(
    plugin,
    event: AstrMessageEvent,
    msg: dict,
    matched_keyword: str,
) -> bool:
    """Process message under keyword trigger (Route 3).
    Returns True if reply is triggered, False otherwise.
    """
    group_id = str(event.get_group_id())
    try:
        plugin.logger.info(
            f"[Keyword] Keyword '{matched_keyword}' matched in group {group_id}."
        )

        if plugin.config_helper.enable_keyword_llm_judgment():
            context_lines = ["=== 群聊中的最近消息 ==="]
            all_image_urls = None
            gcc = plugin.get_group_chat_context()
            if gcc:
                records = list(gcc.raw_records.get(event.unified_msg_origin, []))
                for record in records[-10:]:
                    context_lines.append(record)
            context_text = "\n".join(context_lines)

            persona_name = ""
            try:
                personality = (
                    await plugin.context.persona_manager.get_default_persona_v3(
                        event.unified_msg_origin
                    )
                )
                if personality:
                    persona_name = personality.get("name") or ""
            except Exception:
                pass

            plugin.logger.info(
                f"[Keyword] Calling LLM analyzer to judge if Bot should join the conversation for keyword '{matched_keyword}' in group {group_id}..."
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
            should_join = analysis.get("should_join", False)
            if isinstance(should_join, str):
                should_join = should_join.strip().lower() in ("true", "yes")
            reason = analysis.get("reason", "")
            if not should_join:
                plugin.logger.info(
                    f"[Keyword] Group {group_id} does not warrant participation for keyword '{matched_keyword}' ({reason})"
                )
                return False
            plugin.logger.info(
                f"[Keyword] Group {group_id} approved for participation for keyword '{matched_keyword}' | Reason: {reason}"
            )

        plugin.logger.info(
            f"[Keyword] Triggering native reply for keyword '{matched_keyword}' in group {group_id}..."
        )
        await maybe_typing_delay(plugin)
        return True
    except Exception as e:
        plugin.logger.exception(f"[Keyword] Error in handle_keyword: {e}")
        return False


async def process_group_message(plugin, event: AstrMessageEvent) -> None:
    """Listen to all group messages to collect replies or initiate proactive participation."""
    if not is_group_event(event):
        return

    was_at_or_wake = event.is_at_or_wake_command

    group_id = str(event.get_group_id())
    umo = event.unified_msg_origin
    if not plugin.config_helper.is_group_allowed(group_id, umo):
        return

    cmd_text = (event.message_str or "").strip()
    if cmd_text:
        for prefix in plugin.config_helper.filter_prefixes():
            if cmd_text.startswith(prefix):
                return  # 指令消息，跳过全部处理

    # Check if there are other COMMAND handlers to avoid intercepting them
    activated_handlers = event.get_extra("activated_handlers", [])
    has_other_commands = False
    for handler in activated_handlers:
        if handler.handler_name == "on_group_message":
            continue
        is_cmd = any(
            f.__class__.__name__ in ("CommandFilter", "CommandGroupFilter")
            for f in handler.event_filters
        )
        if is_cmd:
            has_other_commands = True
            break
    if has_other_commands:
        return

    now = time.time()

    # Intercept the native wake/at-bot LLM trigger only during an active tracking window
    tracker = plugin.tracker_manager.get_tracker(group_id)
    if tracker and tracker.alive and now <= tracker.expire_at:
        event.is_at_or_wake_command = False

    is_bot = False
    try:
        self_id = event.get_self_id()
        sender_id = event.get_sender_id()
        if self_id and sender_id and str(sender_id) == str(self_id):
            is_bot = True
    except (AttributeError, TypeError, ValueError):
        pass

    # Check if the message is @ing the bot (by ID or name) or mentioning the bot's name/ID
    is_at_bot = False
    is_at_other = False
    msg_text = event.message_str or ""
    self_id = event.get_self_id()

    persona_name = ""
    personality = None
    try:
        personality = await plugin.context.persona_manager.get_default_persona_v3(umo)
        if personality:
            persona_name = personality.get("name") or ""
    except Exception:
        pass

    # 1. Check message components
    for comp in event.get_messages():
        if isinstance(comp, At):
            at_target = str(getattr(comp, "qq", getattr(comp, "target", "")))
            if at_target == str(self_id):
                is_at_bot = True
            elif (
                persona_name
                and persona_name != "default"
                and getattr(comp, "name", "")
                and persona_name.lower() in getattr(comp, "name", "").lower()
            ):
                is_at_bot = True
            elif at_target and at_target != "all":
                is_at_other = True
        elif isinstance(comp, Reply):
            if str(comp.sender_id) == str(self_id):
                is_at_bot = True
            else:
                is_at_other = True

    # 2. Check if the text contains bot's name/nickname or self_id
    if not is_at_bot:
        if self_id and str(self_id) in msg_text:
            is_at_bot = True
        elif (
            persona_name
            and persona_name != "default"
            and persona_name.lower() in msg_text.lower()
        ):
            is_at_bot = True

    msg_content = event.message_str or ""
    if not msg_content.strip():
        msg_content = event.get_message_outline()

    # If the bot is @-ed but the @ tag was stripped as a wake prefix, prepend it to help LLM recognize it
    if is_at_bot:
        bot_tag = f"@{persona_name or 'Bot'}"
        if bot_tag not in msg_content and f"@{self_id}" not in msg_content:
            msg_content = f"{bot_tag} {msg_content}"

    image_urls = await extract_image_urls(event)
    if (
        image_urls
        and plugin.config_helper.enable_image_caption()
        and plugin.config_helper.enable_keyword_on_image()
    ):
        captions = []
        for url in image_urls:
            caption = await get_image_caption(plugin, url, umo)
            if caption:
                captions.append(caption)
        if captions:
            msg_content += " " + " ".join(f"[图片描述: {cap}]" for cap in captions)

    user_name = event.get_sender_name()
    if is_bot:
        user_name = "你"

    msg = {
        "user_name": user_name,
        "user_id": str(event.get_sender_id()),
        "content": msg_content,
        "image_urls": image_urls,
        "time": now,
        "is_at_bot": is_at_bot,
        "is_at_other": is_at_other and not is_at_bot,
        "is_wake": was_at_or_wake,
    }

    # History is tracked by native GroupChatContext
    if not is_bot:
        await post_process_group_context(plugin, event, image_urls)

    if is_bot:
        return

    # ====== Human-like Mode: schedule + state check ======
    if plugin.config_helper.human_like_mode():
        await plugin._ensure_schedule(group_id, umo, personality)
        state = plugin.tracker_manager.get_state(group_id)
        activity = state.get("activity", 1.0)
        if activity == 0:
            if is_at_bot:
                hits = plugin.tracker_manager.add_wake_hit(
                    group_id, now, plugin.config_helper.wake_window_minutes()
                )
                plugin.logger.info(
                    f"[HumanMode] {group_id} is sleeping, @ hit {hits}/{plugin.config_helper.wake_at_threshold()}"
                )
                if hits >= plugin.config_helper.wake_at_threshold():
                    plugin.logger.info(
                        f"[HumanMode] {group_id} woken up by repeated @s"
                    )
                    plugin.tracker_manager.set_state(
                        group_id,
                        {
                            "name": "空闲",
                            "activity": 1.0,
                            "reason": "被@吵醒了",
                            "manual": True,
                        },
                    )
                    plugin.tracker_manager.clear_wake_hits(group_id)
                else:
                    return
            else:
                return

    # ====== Keyword Trigger (Route 3) ======
    has_active_tracker = False
    tracker = plugin.tracker_manager.get_tracker(group_id)
    if tracker and tracker.alive and now <= tracker.expire_at:
        has_active_tracker = True

    if (
        not has_active_tracker
        and plugin.config_helper.enable_keyword_trigger()
        and plugin.config_helper.parsed_keywords
    ):
        matched_keyword, matched_prob = plugin.config_helper.get_matched_keyword(
            group_id, msg_content
        )
        if matched_prob is None:
            matched_prob = plugin.config_helper.keyword_default_probability()

        if matched_keyword is not None:
            plugin.logger.info(
                f"[Keyword] Matched keyword '{matched_keyword}' in group {group_id}, matched_prob={matched_prob}%."
            )
            if is_probability_hit(matched_prob):
                if not (
                    plugin.tracker_manager.is_active_thinking(group_id)
                    or plugin.tracker_manager.is_proactive_flagged(group_id)
                ):
                    plugin.tracker_manager.set_active_thinking(group_id, True)
                    try:
                        res = await handle_keyword(
                            plugin, event, msg, matched_keyword
                        )
                        if res:
                            event.is_at_or_wake_command = True
                            event.set_extra("chat_echo_triggered", True)
                            event.set_extra("chat_echo_mode", "keyword")
                            event.set_extra(
                                "chat_echo_matched_keyword", matched_keyword
                            )
                            event.set_extra(
                                "selected_provider",
                                plugin.config_helper.generator_provider(),
                            )
                            return
                    finally:
                        plugin.tracker_manager.set_active_thinking(group_id, False)
            else:
                plugin.logger.info(
                    f"[Keyword] Keyword '{matched_keyword}' matched but probability roll missed."
                )

    # ====== Reply Mode (Route 1) - Batch or Instant ======
    tracker = plugin.tracker_manager.get_tracker(group_id)
    if tracker and tracker.alive:
        if now > tracker.expire_at:
            plugin.tracker_manager.cleanup_tracker(group_id)
            # Clear any pending proactive buffer after tracker cleanup
            plugin.tracker_manager.clear_proactive_buffer(group_id)
        else:
            tracker.expire_at = now + plugin.config_helper.track_timeout()

            # History is tracked by native GroupChatContext

            if tracker.analyzing or plugin.tracker_manager.is_active_thinking(group_id):
                return

            # Start background caption for message images (fire-and-forget)
            if plugin.config_helper.enable_image_caption() and image_urls:
                await prewarm_captions(plugin, msg, umo)

            if not plugin.config_helper.batch_analysis_enabled():
                # ---- Legacy instant analysis ----
                should_analyze = (
                    is_probability_hit(
                        plugin.config_helper.get_effective_reply_prob(group_id, umo)
                    )
                    or was_at_or_wake
                    or is_at_bot
                )

                if should_analyze:
                    tracker.analyzing = True
                    res = await handle_reply(plugin, tracker, event)
                    if res:
                        event.is_at_or_wake_command = True
                        event.set_extra("chat_echo_triggered", True)
                        event.set_extra("chat_echo_mode", "reply")
                        event.set_extra(
                            "selected_provider",
                            plugin.config_helper.generator_provider(),
                        )
                        plugin.tracker_manager.set_active_thinking(group_id, True)
                        return
                    return
                return

            # ---- Batch analysis mode ----
            tracker.batch_mode = "reply"
            trigger_now = plugin.tracker_manager.add_to_batch(tracker, msg, plugin)

            if trigger_now:
                # Immediate flush: @bot or batch full
                plugin.logger.info(
                    f"[Batch] Immediate flush triggered by {trigger_now['reason']} in group {group_id}"
                )
                await flush_batch_reply(plugin, tracker, event, group_id, umo)
                return

            # Schedule or check dynamic silence
            if tracker.batch_timer and not tracker.batch_timer.done():
                tracker.batch_timer.cancel()
                tracker.batch_timer = None

            silence_delay = plugin.tracker_manager.compute_silence_delay(
                tracker, plugin
            )
            # Respect absolute timeout
            max_wait = plugin.config_helper.max_batch_wait_seconds()
            elapsed = now - tracker.batch_first_msg_time
            remaining = max(0.5, min(silence_delay, max_wait - elapsed))
            plugin.logger.debug(
                f"[Batch] Scheduling flush in {remaining:.1f}s for group {group_id} (silence={silence_delay:.1f}s, max_wait={max_wait}s)"
            )
            tracker.batch_timer = asyncio.create_task(
                schedule_batch_flush_reply(
                    plugin, tracker, event, group_id, umo, remaining
                )
            )
            return

    # ====== Proactive Mode (Route 2) - Batch or Instant ======
    if plugin.tracker_manager.is_active_thinking(
        group_id
    ) or plugin.tracker_manager.is_proactive_flagged(group_id):
        return
    active_prob = plugin.config_helper.get_effective_active_prob(group_id, umo)
    if active_prob <= 0:
        return
    last_active = plugin.tracker_manager.get_active_cooldown(group_id)
    if now - last_active < plugin.config_helper.proactive_cooldown():
        return
    rounds = plugin.tracker_manager.get_proactive_rounds(group_id)
    if rounds >= plugin.config_helper.max_rounds():
        return
    if plugin.tracker_manager.has_active_tracker(group_id):
        return

    if not is_probability_hit(active_prob):
        return

    if not plugin.config_helper.batch_analysis_enabled():
        # ---- Legacy instant proactive ----
        plugin.tracker_manager.set_active_thinking(group_id, True)
        res = await handle_proactive(plugin, event, msg)
        if res:
            event.is_at_or_wake_command = True
            event.set_extra("chat_echo_triggered", True)
            event.set_extra("chat_echo_mode", "proactive")
            event.set_extra(
                "selected_provider", plugin.config_helper.generator_provider()
            )
            return
        else:
            plugin.tracker_manager.set_active_thinking(group_id, False)
        return

    # ---- Batch proactive mode ----
    # Start background caption for message images
    if plugin.config_helper.enable_image_caption() and image_urls:
        await prewarm_captions(plugin, msg, umo)

    trigger_now = plugin.tracker_manager.add_to_proactive_batch(group_id, msg, plugin)
    if trigger_now:
        plugin.logger.info(
            f"[ProactiveBatch] Immediate flush triggered by {trigger_now['reason']} in group {group_id}"
        )
        await flush_batch_proactive(plugin, event, group_id, umo)
        return

    # Schedule proactive batch flush
    buf = plugin.tracker_manager.get_proactive_buffer(group_id)
    if buf and buf.get("timer") and not buf["timer"].done():
        buf["timer"].cancel()
        buf["timer"] = None

    buf = plugin.tracker_manager.ensure_proactive_buffer(group_id, umo)
    buf["event"] = event
    msg_count = len(buf["buffer"])
    if msg_count <= 1:
        silence_delay = float(plugin.config_helper.min_silence_seconds())
    else:
        total_span = now - buf["first_msg_time"]
        avg_interval = total_span / (msg_count - 1)
        threshold = avg_interval * plugin.config_helper.silence_multiplier()
        silence_delay = max(
            float(plugin.config_helper.min_silence_seconds()),
            min(threshold, float(plugin.config_helper.max_silence_seconds())),
        )

    max_wait = plugin.config_helper.max_batch_wait_seconds()
    elapsed = now - buf["first_msg_time"]
    remaining = max(0.5, min(silence_delay, max_wait - elapsed))
    plugin.logger.debug(
        f"[ProactiveBatch] Scheduling flush in {remaining:.1f}s for group {group_id}"
    )
    buf["timer"] = asyncio.create_task(
        schedule_batch_flush_proactive(plugin, group_id, remaining)
    )
