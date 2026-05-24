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
