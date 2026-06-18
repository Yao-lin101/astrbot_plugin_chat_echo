import asyncio
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
        "bot_message_sent",
        "last_llm_text",
        "last_llm_time",
        # Batch analysis fields
        "batch_buffer",
        "batch_first_msg_time",
        "batch_last_msg_time",
        "batch_timer",
        "batch_caption_tasks",
        "batch_mode",
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
        self.bot_message_sent = False
        self.last_llm_text = ""
        self.last_llm_time = 0.0
        # Batch analysis state
        self.batch_buffer: list[dict] = []
        self.batch_first_msg_time = 0.0
        self.batch_last_msg_time = 0.0
        self.batch_timer: asyncio.Task | None = None
        self.batch_caption_tasks: list[asyncio.Task] = []
        self.batch_mode = "reply"  # "reply" | "proactive" | "keyword"


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
        # Proactive batch buffers (no ConversationTracker for proactive mode)
        self._proactive_buffers: dict[str, dict] = {}

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
            self._cancel_batch_timer(tracker)
            tracker.alive = False

    def _cancel_batch_timer(self, tracker: ConversationTracker) -> None:
        if tracker.batch_timer and not tracker.batch_timer.done():
            tracker.batch_timer.cancel()
        tracker.batch_timer = None

    # ======== Batch analysis helpers ========

    def add_to_batch(
        self,
        tracker: ConversationTracker,
        msg: dict,
        plugin,
    ) -> dict | None:
        """Add a message to the batch buffer.
        Returns trigger_info if batch should be flushed immediately (@bot instant),
        otherwise None.
        """
        now = time.time()
        tracker.batch_buffer.append(msg)
        tracker.batch_last_msg_time = now
        if tracker.batch_first_msg_time == 0:
            tracker.batch_first_msg_time = now
        tracker.expire_at = now + plugin.config_helper.track_timeout()

        # Check if accumulation count exceeds max
        if len(tracker.batch_buffer) >= plugin.config_helper.max_batch_messages():
            return {"reason": "batch_full", "at": now}

        # Check if @bot or wake command triggers instant flush (reply mode only)
        if (
            (msg.get("is_at_bot") or msg.get("is_wake"))
            and plugin.config_helper.instant_at_bot()
            and tracker.batch_mode == "reply"
        ):
            return {
                "reason": "at_bot" if msg.get("is_at_bot") else "wake_prefix",
                "at": now,
            }

        return None

    def compute_silence_delay(self, tracker: ConversationTracker, plugin) -> float:
        """Compute dynamic silence threshold for this batch."""
        if len(tracker.batch_buffer) <= 1:
            # Only one message in buffer, use minimum silence
            return float(plugin.config_helper.min_silence_seconds())

        now = time.time()
        total_span = now - tracker.batch_first_msg_time
        msg_count = len(tracker.batch_buffer)
        avg_interval = total_span / max(msg_count - 1, 1)

        multiplier = plugin.config_helper.silence_multiplier()
        threshold = avg_interval * multiplier

        min_s = float(plugin.config_helper.min_silence_seconds())
        max_s = float(plugin.config_helper.max_silence_seconds())
        return max(min_s, min(threshold, max_s))

    def should_flush_batch(self, tracker: ConversationTracker, plugin) -> bool:
        """Check if the current batch should be flushed based on silence / timeout."""
        now = time.time()

        # Absolute timeout from first message
        max_wait = plugin.config_helper.max_batch_wait_seconds()
        if now - tracker.batch_first_msg_time >= max_wait:
            return True

        # Dynamic silence threshold
        silence_delay = self.compute_silence_delay(tracker, plugin)
        if now - tracker.batch_last_msg_time >= silence_delay:
            return True

        return False

    def get_proactive_buffer(self, group_id: str) -> dict | None:
        return self._proactive_buffers.get(group_id)

    def ensure_proactive_buffer(self, group_id: str, umo: str) -> dict:
        if group_id not in self._proactive_buffers:
            self._proactive_buffers[group_id] = {
                "buffer": [],
                "umo": umo,
                "first_msg_time": 0.0,
                "last_msg_time": 0.0,
                "timer": None,
            }
        return self._proactive_buffers[group_id]

    def add_to_proactive_batch(self, group_id: str, msg: dict, plugin) -> dict | None:
        """Add message to proactive batch buffer. Returns trigger_info or None."""
        now = time.time()
        buf = self.ensure_proactive_buffer(group_id, "")
        buf["buffer"].append(msg)
        buf["last_msg_time"] = now
        if buf["first_msg_time"] == 0:
            buf["first_msg_time"] = now

        if len(buf["buffer"]) >= plugin.config_helper.max_batch_messages():
            return {"reason": "batch_full", "at": now}
        return None

    def should_flush_proactive(self, group_id: str, plugin) -> bool:
        """Check if proactive batch should be flushed."""
        buf = self._proactive_buffers.get(group_id)
        if not buf or not buf["buffer"]:
            return False
        now = time.time()

        max_wait = plugin.config_helper.max_batch_wait_seconds()
        if now - buf["first_msg_time"] >= max_wait:
            return True

        # Dynamic silence
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

        return now - buf["last_msg_time"] >= silence_delay

    def clear_proactive_buffer(self, group_id: str) -> list[dict]:
        buf = self._proactive_buffers.pop(group_id, None)
        if buf:
            t = buf.get("timer")
            if t and not t.done():
                t.cancel()
            return buf["buffer"]
        return []

    def clear_batch_state(self, tracker: ConversationTracker) -> list[dict]:
        """Clear batch buffer and return collected messages for analysis."""
        self._cancel_batch_timer(tracker)
        batch = tracker.batch_buffer
        tracker.batch_buffer = []
        tracker.batch_first_msg_time = 0.0
        tracker.batch_last_msg_time = 0.0
        tracker.batch_timer = None
        return batch

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
        ts = self.active_thinking.get(group_id)
        if not ts:
            return False
        if isinstance(ts, float) and time.time() - ts > 60.0:
            self.active_thinking[group_id] = None
            return False
        return True

    def set_active_thinking(self, group_id: str, value: bool) -> None:
        if value:
            self.active_thinking[group_id] = time.time()
        else:
            self.active_thinking[group_id] = None

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
            self._cancel_batch_timer(tracker)
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
        # Clean proactive buffers
        for buf in self._proactive_buffers.values():
            t = buf.get("timer")
            if t and not t.done():
                t.cancel()
        self._proactive_buffers.clear()

    # ======== Human-like mode helpers ========

    def get_state(self, group_id: str) -> dict:
        """Get current state for a group. Default: free/1.0."""
        return self._current_state.get(
            group_id, {"name": "空闲", "activity": 1.0, "reason": ""}
        )

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
