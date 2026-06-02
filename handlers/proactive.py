import time

from astrbot.api.event import AstrMessageEvent

from ..helpers import maybe_typing_delay
from ..services.image_caption import ensure_context_captions


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
        await maybe_typing_delay(plugin)
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
        await maybe_typing_delay(plugin)
        return True

    except Exception as e:
        plugin.logger.exception(
            f"[ProactiveBatch] Error in handle_proactive_batch: {e}"
        )
        return False
    finally:
        plugin.tracker_manager.set_active_thinking(group_id, False)
