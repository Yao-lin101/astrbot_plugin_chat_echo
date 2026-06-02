import asyncio

from .proactive import handle_proactive_batch
from .reply import handle_reply_batch


async def schedule_batch_flush_reply(plugin, tracker, event, group_id, umo, delay):
    """Wait for dynamic silence period, then flush reply batch."""
    try:
        await asyncio.sleep(delay)
        if not tracker.alive:
            return
        if tracker.analyzing or plugin.tracker_manager.is_active_thinking(group_id):
            return
        if not tracker.batch_buffer:
            return

        plugin.logger.info(
            f"[Batch] Flushing reply batch ({len(tracker.batch_buffer)} msgs) in group {group_id}"
        )
        await flush_batch_reply(plugin, tracker, event, group_id, umo)
    except asyncio.CancelledError:
        pass
    except Exception:
        plugin.logger.exception("[Batch] Error in scheduled reply flush")


async def flush_batch_reply(plugin, tracker, event, group_id, umo):
    """Flush accumulated batch messages for reply analysis and trigger if appropriate."""
    if tracker.analyzing or plugin.tracker_manager.is_active_thinking(group_id):
        return

    batch = plugin.tracker_manager.clear_batch_state(tracker)
    if not batch:
        return

    tracker.analyzing = True
    try:
        res = await handle_reply_batch(plugin, tracker, event, batch)
        if res:
            event.is_at_or_wake_command = True
            event.set_extra("chat_echo_triggered", True)
            event.set_extra("chat_echo_mode", "reply")
            event.set_extra(
                "selected_provider", plugin.config_helper.generator_provider()
            )
            plugin.tracker_manager.set_active_thinking(group_id, True)
    finally:
        tracker.analyzing = False


async def schedule_batch_flush_proactive(plugin, group_id, delay):
    """Wait for dynamic silence period, then flush proactive batch."""
    try:
        await asyncio.sleep(delay)
        buf = plugin.tracker_manager.get_proactive_buffer(group_id)
        if not buf or not buf["buffer"]:
            return
        if plugin.tracker_manager.is_active_thinking(group_id):
            return

        event = buf.get("event")
        umo = buf.get("umo", "")
        plugin.logger.info(
            f"[ProactiveBatch] Flushing proactive batch ({len(buf['buffer'])} msgs) in group {group_id}"
        )
        if event:
            await flush_batch_proactive(plugin, event, group_id, umo)
    except asyncio.CancelledError:
        return
    except Exception:
        plugin.logger.exception("[ProactiveBatch] Error in scheduled proactive flush")
        return


async def flush_batch_proactive(plugin, event, group_id, umo):
    """Flush accumulated proactive batch for participation analysis."""
    if plugin.tracker_manager.is_active_thinking(group_id):
        return

    batch = plugin.tracker_manager.clear_proactive_buffer(group_id)
    if not batch:
        return

    plugin.tracker_manager.set_active_thinking(group_id, True)
    try:
        res = await handle_proactive_batch(plugin, event, batch)
        if res:
            event.is_at_or_wake_command = True
            event.set_extra("chat_echo_triggered", True)
            event.set_extra("chat_echo_mode", "proactive")
            event.set_extra(
                "selected_provider", plugin.config_helper.generator_provider()
            )
    finally:
        plugin.tracker_manager.set_active_thinking(group_id, False)
