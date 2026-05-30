import time


class ConversationTracker:
    """Tracks the state of a group chat conversation."""

    __slots__ = (
        "group_id",
        "unified_msg_origin",
        "bot_message",
        "trigger_user_name",
        "trigger_user_id",
        "trigger_message",
        "collected",
        "expire_at",
        "analyzing",
        "detection_count",
        "alive",
        "group_name",
    )

    def __init__(
        self,
        group_id: str,
        unified_msg_origin: str,
        bot_message: str,
        trigger_user_name: str,
        trigger_user_id: str,
        trigger_message: str,
        expire_seconds: int,
    ):
        self.group_id = group_id
        self.unified_msg_origin = unified_msg_origin
        self.bot_message = bot_message
        self.trigger_user_name = trigger_user_name
        self.trigger_user_id = trigger_user_id
        self.trigger_message = trigger_message
        self.collected: list[dict] = []
        self.expire_at = time.time() + expire_seconds
        self.analyzing = False
        self.detection_count = 0
        self.alive = True
        self.group_name = ""


class TrackerManager:
    """Manages active conversation trackers, cooldowns, and message windows."""

    def __init__(self):
        self.trackers: dict[str, ConversationTracker] = {}
        self.recent_messages: dict[str, list[dict]] = {}
        self.active_thinking: dict[str, bool] = {}
        self.proactive_flag: dict[str, bool] = {}
        self.active_cooldowns: dict[str, float] = {}
        self.proactive_rounds: dict[str, int] = {}
        self.cooldowns: dict[str, float] = {}
        # Human-like mode state
        self._current_state: dict[str, dict] = {}
        self._schedule: dict[str, list[dict]] = {}
        self._schedule_timer: dict[str, object] = {}
        self._wake_hits: dict[str, list[float]] = {}
        self._last_schedule_check: dict[str, float] = {}

    def get_tracker(self, group_id: str) -> ConversationTracker | None:
        return self.trackers.get(group_id)

    def has_active_tracker(self, group_id: str) -> bool:
        tracker = self.get_tracker(group_id)
        return tracker is not None and tracker.alive

    def start_tracking(
        self,
        group_id: str,
        unified_msg_origin: str,
        bot_message: str,
        trigger_user_name: str,
        trigger_user_id: str,
        trigger_message: str,
        expire_seconds: int,
        group_name: str = "",
    ) -> ConversationTracker:
        tracker = ConversationTracker(
            group_id=group_id,
            unified_msg_origin=unified_msg_origin,
            bot_message=bot_message,
            trigger_user_name=trigger_user_name,
            trigger_user_id=trigger_user_id,
            trigger_message=trigger_message,
            expire_seconds=expire_seconds,
        )
        if group_name:
            tracker.group_name = group_name
        self.trackers[group_id] = tracker
        return tracker

    def cleanup_tracker(self, group_id: str) -> None:
        tracker = self.trackers.pop(group_id, None)
        if tracker:
            tracker.alive = False

    def add_to_recent(self, group_id: str, msg: dict, max_size: int) -> list[dict]:
        if group_id not in self.recent_messages:
            self.recent_messages[group_id] = []
        window = self.recent_messages[group_id]
        window.append(msg)
        if len(window) > max_size:
            window.pop(0)
        return window

    def get_recent(self, group_id: str) -> list[dict]:
        return self.recent_messages.get(group_id, [])

    def is_active_thinking(self, group_id: str) -> bool:
        return bool(self.active_thinking.get(group_id))

    def set_active_thinking(self, group_id: str, value: bool) -> None:
        self.active_thinking[group_id] = value

    def is_proactive_flagged(self, group_id: str) -> bool:
        return bool(self.proactive_flag.get(group_id))

    def set_proactive_flag(self, group_id: str, value: bool) -> None:
        self.proactive_flag[group_id] = value

    def get_active_cooldown(self, group_id: str) -> float:
        return self.active_cooldowns.get(group_id, 0.0)

    def set_active_cooldown(self, group_id: str, timestamp: float) -> None:
        self.active_cooldowns[group_id] = timestamp

    def get_proactive_rounds(self, group_id: str) -> int:
        return self.proactive_rounds.get(group_id, 0)

    def increment_proactive_rounds(self, group_id: str) -> int:
        rounds = self.get_proactive_rounds(group_id) + 1
        self.proactive_rounds[group_id] = rounds
        return rounds

    def clear_all(self) -> None:
        for tracker in list(self.trackers.values()):
            tracker.alive = False
        self.trackers.clear()
        self.recent_messages.clear()
        self.active_thinking.clear()
        self.proactive_flag.clear()
        self.active_cooldowns.clear()
        self.proactive_rounds.clear()
        self.cooldowns.clear()
        self._current_state.clear()
        self._schedule.clear()
        for t in self._schedule_timer.values():
            try:
                t.cancel()
            except Exception:
                pass
        self._schedule_timer.clear()
        self._wake_hits.clear()
        self._last_schedule_check.clear()

    # ======== Human-like mode helpers ========

    def get_state(self, group_id: str) -> dict:
        """Get current state for a group. Default: free/1.0."""
        return self._current_state.get(group_id, {"name": "空闲", "activity": 1.0, "reason": ""})

    def set_state(self, group_id: str, state: dict) -> None:
        self._current_state[group_id] = state

    def get_schedule(self, group_id: str) -> list[dict]:
        return self._schedule.get(group_id, [])

    def set_schedule(self, group_id: str, schedule: list[dict]) -> None:
        self._schedule[group_id] = schedule

    def cancel_schedule_timer(self, group_id: str) -> None:
        t = self._schedule_timer.pop(group_id, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

    def set_schedule_timer(self, group_id: str, task) -> None:
        self._schedule_timer[group_id] = task

    def get_last_schedule_check(self, group_id: str) -> float:
        return self._last_schedule_check.get(group_id, 0.0)

    def set_last_schedule_check(self, group_id: str, ts: float) -> None:
        self._last_schedule_check[group_id] = ts

    def add_wake_hit(self, group_id: str, now: float, window_minutes: int) -> int:
        """Add an @ hit, clean old ones, return current count."""
        hits = self._wake_hits.setdefault(group_id, [])
        cutoff = now - window_minutes * 60
        hits[:] = [h for h in hits if h > cutoff]
        hits.append(now)
        return len(hits)

    def clear_wake_hits(self, group_id: str) -> None:
        self._wake_hits.pop(group_id, None)

    def clear_all_wake(self) -> None:
        self._wake_hits.clear()
