import asyncio
import json
import re
from datetime import datetime, timedelta

from ..helpers import extract_bot_text


async def ensure_schedule(plugin, group_id: str, umo: str, personality) -> None:
    """Ensure a schedule exists for this group, refresh when exhausted."""
    schedule = plugin.tracker_manager.get_schedule(group_id)
    if schedule:
        last_until = schedule[-1].get("until", "23:59")
        first_until = schedule[0].get("until", "00:00")
        now_dt = datetime.now()
        current_minutes = now_dt.hour * 60 + now_dt.minute
        try:
            h1, m1 = map(int, first_until.split(":"))
            h2, m2 = map(int, last_until.split(":"))
            last_minutes = h2 * 60 + m2
            if last_minutes < h1 * 60 + m1:
                last_minutes += 24 * 60
            if current_minutes < last_minutes:
                return
        except (ValueError, AttributeError):
            pass
        plugin.tracker_manager.cancel_schedule_timer(group_id)
    try:
        persona_name_str = ""
        if personality and personality.get("name"):
            persona_name_str = personality["name"].strip()
        now_dt = datetime.now()
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekdays[now_dt.weekday()]
        prompt = (
            f"你Bot的人格是：{persona_name_str}\n\n"
            f"当前时间：{now_dt.strftime('%Y-%m-%d %H:%M')}，星期{weekday}\n\n"
            "根据你的人设，请规划接下来一段时间的行为状态。\n"
            "每个时段定义：状态名、活跃度(0.0-1.0)、结束时间、简短理由。\n"
            "活跃度含义：1.0=完全在线积极参与、0.5=偶尔看看、0.0=不可用(如睡觉)\n\n"
            '输出JSON数组：[{"state": "状态名", "activity": 0.0-1.0, "until": "HH:MM", "reason": "简短理由"}]\n'
            "只输出JSON，不要其他内容。直接输出，不要思考过程。只需要规划接下来12小时即可。"
        )
        provider_id = plugin.config_helper.analyzer_provider()
        if not provider_id and umo:
            try:
                provider_id = await plugin.context.get_current_chat_provider_id(umo)
            except Exception:
                pass
        if not provider_id:
            return
        resp = await plugin.context.llm_generate(
            prompt=prompt,
            chat_provider_id=provider_id,
        )
        text = extract_bot_text(resp) if resp else None
        if not text:
            return
        # Parse JSON
        try:
            schedule = json.loads(text.strip())
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if m:
                try:
                    schedule = json.loads(m.group())
                except json.JSONDecodeError:
                    return
            else:
                return
        if not isinstance(schedule, list) or len(schedule) == 0:
            return
        plugin.tracker_manager.set_schedule(group_id, schedule)
        plugin.tracker_manager.cancel_schedule_timer(group_id)
        apply_schedule(plugin, group_id, schedule, now_dt)
        plugin.logger.info(
            f"[HumanMode] {group_id} schedule refreshed: {len(schedule)} items"
        )
    except Exception as e:
        plugin.logger.exception(f"[HumanMode] Failed to generate schedule: {e}")


def apply_schedule(plugin, group_id: str, schedule: list, now_dt: datetime) -> None:
    """Set current state from schedule and start timer for next transition."""
    today = now_dt.date()
    current_match = None
    next_item = None
    current_minutes = now_dt.hour * 60 + now_dt.minute
    for item in schedule:
        try:
            until = item.get("until", "23:59")
            h, m = map(int, until.split(":"))
            item_minutes = h * 60 + m
            if item_minutes > current_minutes:
                if next_item is None:
                    next_item = item
                if current_match is None:
                    current_match = item
            elif current_match is None:
                current_match = item
        except (ValueError, AttributeError):
            continue
    if current_match is None and schedule:
        current_match = schedule[-1]
    current_state = plugin.tracker_manager.get_state(group_id)
    if current_state.get("manual"):
        plugin.tracker_manager.set_state(group_id, {**current_state, "manual": False})
    elif current_match:
        plugin.tracker_manager.set_state(
            group_id,
            {
                "name": current_match.get("state", "空闲"),
                "activity": float(current_match.get("activity", 1.0)),
                "reason": current_match.get("reason", ""),
            },
        )
        plugin.logger.info(
            f"[HumanMode] {group_id} state: {current_match.get('state')} (activity={current_match.get('activity')})"
        )
    # Schedule next transition
    if next_item:
        try:
            h, m = map(int, next_item["until"].split(":"))
            target = datetime(today.year, today.month, today.day, h, m)
            if target <= now_dt:
                target += timedelta(days=1)
            delay = (target - datetime.now()).total_seconds()
            if delay > 0:

                async def _transition():
                    await asyncio.sleep(delay)
                    dt = datetime.now()
                    apply_schedule(plugin, group_id, schedule, dt)

                task = asyncio.create_task(_transition())
                plugin.tracker_manager.set_schedule_timer(group_id, task)
        except (ValueError, AttributeError):
            pass
