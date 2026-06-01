"""
AstrBot 主动接话插件 (astrbot_plugin_chat_echo)

Refactored for improved maintainability.
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

from .config import ConfigHelper, upgrade_config
from .handlers import (
    handle_keyword,
    handle_proactive,
    handle_proactive_batch,
    handle_reply,
    handle_reply_batch,
    prewarm_captions,
    start_tracking,
)
from .helpers import (
    extract_bot_text,
    extract_image_urls,
    extract_sent_text,
    is_group_event,
    is_probability_hit,
)
from .llm_client import LLMHandler
from .tracker import TrackerManager
from .utils.caption_cache import ImageCaptionCache
from .utils.token_counter import TokenCounter

PLUGIN_NAME = "astrbot_plugin_chat_echo"
PROACTIVE_WINDOW_SIZE = 10


@register("astrbot_plugin_chat_echo", "AMYdd00, Yao-lin101", "主动接话插件", "1.1.3")
class EchoPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger

        data_dir = StarTools.get_data_dir("chat_echo")
        self.token_counter = TokenCounter(data_dir)
        self.caption_cache = ImageCaptionCache(Path(data_dir))

        # Upgrade config prompts and initialize config helper
        upgrade_config(self.config, Path(data_dir), self.logger)
        self.config_helper = ConfigHelper(self.config)

        self.tracker_manager = TrackerManager()
        self.llm_handler = LLMHandler(
            self.context, self.config_helper, self.token_counter, self.logger
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/token_stats",
            self.page_token_stats,
            ["GET"],
            "Token 统计数据",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/token_history",
            self.page_token_history,
            ["GET"],
            "历史趋势数据（多群多线）",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache",
            self.api_caption_cache_list,
            ["GET"],
            "图片转述缓存列表",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/stats",
            self.api_caption_cache_stats,
            ["GET"],
            "图片转述缓存统计",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/delete",
            self.api_caption_cache_delete,
            ["POST"],
            "删除单条转述缓存",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/clear",
            self.api_caption_cache_clear,
            ["POST"],
            "清空全部转述缓存",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/clear_before",
            self.api_caption_cache_clear_before,
            ["POST"],
            "按时间清理转述缓存",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/update",
            self.api_caption_cache_update,
            ["POST"],
            "更新转述内容",
        )

    async def initialize(self):
        self.logger.info(
            f"主动接话插件初始化完成 | 触发模式: {self.config_helper.trigger_mode()} | "
            f"关键词监听: {self.config_helper.enable_keyword_trigger()} (规则数: {len(self.config_helper.parsed_keywords)}) | "
            f"批次分析: {self.config_helper.batch_analysis_enabled()}"
        )
        self.token_counter.start()

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """Triggered after Bot LLM response, starts tracking subsequent group replies."""
        if not is_group_event(event):
            return
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self.config_helper.is_group_allowed(group_id, umo):
            return
        if self.tracker_manager.is_proactive_flagged(group_id):
            return

        chat_echo_triggered = event.get_extra("chat_echo_triggered")
        bot_text = extract_bot_text(response)

        if chat_echo_triggered:
            self.tracker_manager.set_active_thinking(group_id, False)
            tracker = self.tracker_manager.get_tracker(group_id)
            if tracker and tracker.alive:
                tracker.collected.append(
                    {
                        "user_name": "你",
                        "user_id": "bot",
                        "content": bot_text,
                        "image_urls": [],
                        "time": time.time(),
                        "is_at_bot": False,
                    }
                )
                tracker.detection_count = 0
                tracker.expire_at = time.time() + self.config_helper.track_timeout()
            else:
                await start_tracking(self, event, bot_text)
            return

        if self.config_helper.trigger_mode() in ("llm_response", "any_message"):
            await start_tracking(self, event, bot_text)

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Inject tracked conversation history before LLM request is sent."""
        if not event.get_extra("chat_echo_triggered"):
            return

        group_id = str(event.get_group_id())
        tracker = self.tracker_manager.get_tracker(group_id)

        recent_msgs = []
        if tracker:
            recent_msgs = tracker.collected
        else:
            recent_msgs = self.tracker_manager.get_recent(group_id) or []

        injected_contexts = []
        msgs_to_inject = recent_msgs[:-1] if recent_msgs else []

        for msg in msgs_to_inject:
            role = (
                "assistant"
                if msg["user_name"] == "你" or msg["user_id"] == "bot"
                else "user"
            )
            content = msg["content"]
            if role == "user":
                content = f"{msg['user_name']}: {content}"
            injected_contexts.append(
                {"role": role, "content": content, "_no_save": True}
            )

        if injected_contexts:
            event.set_extra("chat_echo_original_contexts", req.contexts)
            req.contexts = injected_contexts
            self.logger.debug(
                f"[ChatEcho] Overwrote LLM contexts with {len(injected_contexts)} tracked group messages."
            )

        mode = event.get_extra("chat_echo_mode")
        if mode == "keyword":
            matched_keyword = event.get_extra("chat_echo_matched_keyword")
            if matched_keyword:
                keyword_hint = f"\n\n[系统提示：用户提到关键词 '{matched_keyword}' 触发了你，请自然地进行接话。]"
                if req.system_prompt is None:
                    req.system_prompt = ""
                req.system_prompt += keyword_hint

    @filter.on_agent_done()
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context,
        response,
    ) -> None:
        """Restore original conversation history before it is saved to the database."""
        if not event.get_extra("chat_echo_triggered"):
            return

        original_contexts = event.get_extra("chat_echo_original_contexts")
        if original_contexts is None:
            return

        from astrbot.core.agent.message import bind_checkpoint_messages

        # Reconstruct the messages list starting with original history
        restored_messages = []
        if run_context.messages and run_context.messages[0].role == "system":
            restored_messages.append(run_context.messages[0])

        restored_messages.extend(bind_checkpoint_messages(original_contexts))

        # Append new user prompt and assistant response, filtering out injected contexts
        for msg in run_context.messages:
            if msg.role == "system":
                continue
            if msg.role in ["user", "assistant"] and getattr(msg, "_no_save", False):
                continue
            restored_messages.append(msg)

        run_context.messages = restored_messages

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """Triggered after Bot sends any message, starts tracking subsequent group replies."""
        if not is_group_event(event):
            return
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self.config_helper.is_group_allowed(group_id, umo):
            return
        if self.tracker_manager.is_proactive_flagged(group_id):
            return
        if self.config_helper.trigger_mode() != "any_message":
            return
        bot_text = extract_sent_text(event)
        await start_tracking(self, event, bot_text)

    @filter.command("bot在干嘛")
    async def cmd_bot_status(self, event: AstrMessageEvent):
        """查询 Bot 当前状态"""
        event.is_at_or_wake_command = True
        if not is_group_event(event):
            yield event.plain_result("此命令仅支持群聊环境")
            return
        if not self.config_helper.human_like_mode():
            yield event.plain_result("伪人模式未开启")
            return
        group_id = str(event.get_group_id())
        if not self.tracker_manager.get_schedule(group_id):
            await self._ensure_schedule(group_id, event.unified_msg_origin, None)
        state = self.tracker_manager.get_state(group_id)
        name = state.get("name", "空闲")
        reason = state.get("reason", "")
        activity = state.get("activity", 1.0)
        msg = (
            f"{name}（活跃度: {activity}）— {reason}"
            if reason
            else f"{name}（活跃度: {activity}）"
        )
        yield event.plain_result(msg)

    @filter.command("bot计划表")
    async def cmd_bot_schedule(self, event: AstrMessageEvent):
        """查询 Bot 计划表"""
        event.is_at_or_wake_command = True
        if not is_group_event(event):
            yield event.plain_result("此命令仅支持群聊环境")
            return
        if not self.config_helper.human_like_mode():
            yield event.plain_result("伪人模式未开启")
            return
        group_id = str(event.get_group_id())
        if not self.tracker_manager.get_schedule(group_id):
            await self._ensure_schedule(group_id, event.unified_msg_origin, None)
        schedule = self.tracker_manager.get_schedule(group_id)
        if not schedule:
            yield event.plain_result("暂无计划表")
            return
        lines = ["Bot 当前计划表："]
        for item in schedule:
            s = item.get("state", "?")
            a = item.get("activity", 0)
            u = item.get("until", "?")
            r = item.get("reason", "")
            lines.append(
                f"{s} (活跃度 {a}) 至 {u} — {r}" if r else f"{s} (活跃度 {a}) 至 {u}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        """Listen to all group messages to collect replies or initiate proactive participation."""
        if not is_group_event(event):
            return

        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self.config_helper.is_group_allowed(group_id, umo):
            return

        cmd_text = (event.message_str or "").strip()
        if cmd_text:
            for prefix in self.config_helper.filter_prefixes():
                if cmd_text.startswith(prefix):
                    return  # 指令消息，跳过全部处理

        now = time.time()

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
        try:
            personality = await self.context.persona_manager.get_default_persona_v3(umo)
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

        image_urls = await extract_image_urls(event)
        if (
            image_urls
            and self.config_helper.enable_image_caption()
            and self.config_helper.enable_keyword_on_image()
        ):
            captions = []
            for url in image_urls:
                caption = await self.get_image_caption(url, umo)
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
        }

        window = self.tracker_manager.add_to_recent(
            group_id, msg, PROACTIVE_WINDOW_SIZE
        )

        if is_bot:
            return

        # ====== Human-like Mode: schedule + state check ======
        if self.config_helper.human_like_mode():
            await self._ensure_schedule(group_id, umo, personality)
            state = self.tracker_manager.get_state(group_id)
            activity = state.get("activity", 1.0)
            if activity == 0:
                if is_at_bot:
                    hits = self.tracker_manager.add_wake_hit(
                        group_id, now, self.config_helper.wake_window_minutes()
                    )
                    self.logger.info(
                        f"[HumanMode] {group_id} is sleeping, @ hit {hits}/{self.config_helper.wake_at_threshold()}"
                    )
                    if hits >= self.config_helper.wake_at_threshold():
                        self.logger.info(
                            f"[HumanMode] {group_id} woken up by repeated @s"
                        )
                        self.tracker_manager.set_state(
                            group_id,
                            {
                                "name": "空闲",
                                "activity": 1.0,
                                "reason": "被@吵醒了",
                                "manual": True,
                            },
                        )
                        self.tracker_manager.clear_wake_hits(group_id)
                        scale = 1.0  # 唤醒后活跃度恢复
                    else:
                        return
                else:
                    return
            elif activity < 1.0:
                scale = activity
            else:
                scale = 1.0
            # Apply activity scaling to route probabilities
            self._activity_scale = scale
        else:
            self._activity_scale = 1.0

        # ====== Keyword Trigger (Route 3) ======
        if (
            self.config_helper.enable_keyword_trigger()
            and self.config_helper.parsed_keywords
        ):
            matched_keyword, matched_prob = self.config_helper.get_matched_keyword(
                group_id, msg_content
            )
            if matched_prob is None:
                matched_prob = self.config_helper.keyword_default_probability()

            if matched_keyword is not None:
                self.logger.info(
                    f"[Keyword] Matched keyword '{matched_keyword}' in group {group_id}, matched_prob={matched_prob}%."
                )
                if is_probability_hit(matched_prob):
                    if not (
                        self.tracker_manager.is_active_thinking(group_id)
                        or self.tracker_manager.is_proactive_flagged(group_id)
                    ):
                        self.tracker_manager.set_active_thinking(group_id, True)
                        try:
                            res = await handle_keyword(
                                self, event, msg, window, matched_keyword
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
                                    self.config_helper.generator_provider(),
                                )
                                return
                        finally:
                            self.tracker_manager.set_active_thinking(group_id, False)
                else:
                    self.logger.info(
                        f"[Keyword] Keyword '{matched_keyword}' matched but probability roll missed."
                    )

        # ====== Reply Mode (Route 1) - Batch or Instant ======
        tracker = self.tracker_manager.get_tracker(group_id)
        if tracker and tracker.alive:
            if now > tracker.expire_at:
                self.tracker_manager.cleanup_tracker(group_id)
                # Clear any pending proactive buffer after tracker cleanup
                self.tracker_manager.clear_proactive_buffer(group_id)
            else:
                tracker.expire_at = now + self.config_helper.track_timeout()

                # Also collect in legacy list (for compatibility with existing handlers)
                tracker.collected.append(msg)

                if tracker.analyzing or self.tracker_manager.is_active_thinking(
                    group_id
                ):
                    return

                # Start background caption for message images (fire-and-forget)
                if self.config_helper.enable_image_caption() and image_urls:
                    await prewarm_captions(self, msg, umo)

                if not self.config_helper.batch_analysis_enabled():
                    # ---- Legacy instant analysis ----
                    if is_probability_hit(
                        self.config_helper.get_effective_reply_prob(group_id, umo)
                    ):
                        tracker.analyzing = True
                        res = await handle_reply(self, tracker, event)
                        if res:
                            event.is_at_or_wake_command = True
                            event.set_extra("chat_echo_triggered", True)
                            event.set_extra("chat_echo_mode", "reply")
                            event.set_extra(
                                "selected_provider",
                                self.config_helper.generator_provider(),
                            )
                            self.tracker_manager.set_active_thinking(group_id, True)
                            return
                        return
                    return

                # ---- Batch analysis mode ----
                tracker.batch_mode = "reply"
                trigger_now = self.tracker_manager.add_to_batch(tracker, msg, self)

                if trigger_now:
                    # Immediate flush: @bot or batch full
                    self.logger.info(
                        f"[Batch] Immediate flush triggered by {trigger_now['reason']} in group {group_id}"
                    )
                    await self._flush_batch_reply(tracker, event, group_id, umo)
                    return

                # Schedule or check dynamic silence
                if tracker.batch_timer and not tracker.batch_timer.done():
                    tracker.batch_timer.cancel()
                    tracker.batch_timer = None

                silence_delay = self.tracker_manager.compute_silence_delay(
                    tracker, self
                )
                # Also respect absolute timeout
                max_wait = self.config_helper.max_batch_wait_seconds()
                elapsed = now - tracker.batch_first_msg_time
                remaining = max(0.5, min(silence_delay, max_wait - elapsed))
                self.logger.debug(
                    f"[Batch] Scheduling flush in {remaining:.1f}s for group {group_id} (silence={silence_delay:.1f}s, max_wait={max_wait}s)"
                )
                tracker.batch_timer = asyncio.create_task(
                    self._schedule_batch_flush_reply(
                        tracker, event, group_id, umo, remaining
                    )
                )
                return

        # ====== Proactive Mode (Route 2) - Batch or Instant ======
        if self.tracker_manager.is_active_thinking(
            group_id
        ) or self.tracker_manager.is_proactive_flagged(group_id):
            return
        active_prob = self.config_helper.get_effective_active_prob(group_id, umo)
        if active_prob <= 0:
            return
        last_active = self.tracker_manager.get_active_cooldown(group_id)
        if now - last_active < self.config_helper.proactive_cooldown():
            return
        rounds = self.tracker_manager.get_proactive_rounds(group_id)
        if rounds >= self.config_helper.max_rounds():
            return
        if self.tracker_manager.has_active_tracker(group_id):
            return

        if not is_probability_hit(active_prob):
            return

        if not self.config_helper.batch_analysis_enabled():
            # ---- Legacy instant proactive ----
            self.tracker_manager.set_active_thinking(group_id, True)
            res = await handle_proactive(self, event, msg, window)
            if res:
                event.is_at_or_wake_command = True
                event.set_extra("chat_echo_triggered", True)
                event.set_extra("chat_echo_mode", "proactive")
                event.set_extra(
                    "selected_provider", self.config_helper.generator_provider()
                )
                return
            else:
                self.tracker_manager.set_active_thinking(group_id, False)
            return

        # ---- Batch proactive mode ----
        # Start background caption for message images
        if self.config_helper.enable_image_caption() and image_urls:
            await prewarm_captions(self, msg, umo)

        trigger_now = self.tracker_manager.add_to_proactive_batch(group_id, msg, self)
        if trigger_now:
            self.logger.info(
                f"[ProactiveBatch] Immediate flush triggered by {trigger_now['reason']} in group {group_id}"
            )
            await self._flush_batch_proactive(event, group_id, umo)
            return

        # Schedule proactive batch flush
        buf = self.tracker_manager.get_proactive_buffer(group_id)
        if buf and buf.get("timer") and not buf["timer"].done():
            buf["timer"].cancel()
            buf["timer"] = None

        buf = self.tracker_manager.ensure_proactive_buffer(group_id, umo)
        buf["event"] = event
        msg_count = len(buf["buffer"])
        if msg_count <= 1:
            silence_delay = float(self.config_helper.min_silence_seconds())
        else:
            total_span = now - buf["first_msg_time"]
            avg_interval = total_span / (msg_count - 1)
            threshold = avg_interval * self.config_helper.silence_multiplier()
            silence_delay = max(
                float(self.config_helper.min_silence_seconds()),
                min(threshold, float(self.config_helper.max_silence_seconds())),
            )

        max_wait = self.config_helper.max_batch_wait_seconds()
        elapsed = now - buf["first_msg_time"]
        remaining = max(0.5, min(silence_delay, max_wait - elapsed))
        self.logger.debug(
            f"[ProactiveBatch] Scheduling flush in {remaining:.1f}s for group {group_id}"
        )
        buf["timer"] = asyncio.create_task(
            self._schedule_batch_flush_proactive(group_id, remaining)
        )

    # ======== Batch flush methods ========

    async def _schedule_batch_flush_reply(self, tracker, event, group_id, umo, delay):
        """Wait for dynamic silence period, then flush reply batch."""
        try:
            await asyncio.sleep(delay)
            if not tracker.alive:
                return
            if tracker.analyzing or self.tracker_manager.is_active_thinking(group_id):
                return
            if not tracker.batch_buffer:
                return

            self.logger.info(
                f"[Batch] Flushing reply batch ({len(tracker.batch_buffer)} msgs) in group {group_id}"
            )
            await self._flush_batch_reply(tracker, event, group_id, umo)
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.exception("[Batch] Error in scheduled reply flush")

    async def _flush_batch_reply(self, tracker, event, group_id, umo):
        """Flush accumulated batch messages for reply analysis and trigger if appropriate."""
        if tracker.analyzing or self.tracker_manager.is_active_thinking(group_id):
            return

        batch = self.tracker_manager.clear_batch_state(tracker)
        if not batch:
            return

        tracker.analyzing = True
        try:
            res = await handle_reply_batch(self, tracker, event, batch)
            if res:
                event.is_at_or_wake_command = True
                event.set_extra("chat_echo_triggered", True)
                event.set_extra("chat_echo_mode", "reply")
                event.set_extra(
                    "selected_provider", self.config_helper.generator_provider()
                )
                self.tracker_manager.set_active_thinking(group_id, True)
        finally:
            tracker.analyzing = False

    async def _schedule_batch_flush_proactive(self, group_id, delay):
        """Wait for dynamic silence period, then flush proactive batch."""
        try:
            await asyncio.sleep(delay)
            buf = self.tracker_manager.get_proactive_buffer(group_id)
            if not buf or not buf["buffer"]:
                return
            if self.tracker_manager.is_active_thinking(group_id):
                return

            event = buf.get("event")
            umo = buf.get("umo", "")
            self.logger.info(
                f"[ProactiveBatch] Flushing proactive batch ({len(buf['buffer'])} msgs) in group {group_id}"
            )
            if event:
                await self._flush_batch_proactive(event, group_id, umo)
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception("[ProactiveBatch] Error in scheduled proactive flush")
            return

    async def _flush_batch_proactive(self, event, group_id, umo):
        """Flush accumulated proactive batch for participation analysis."""
        if self.tracker_manager.is_active_thinking(group_id):
            return

        batch = self.tracker_manager.clear_proactive_buffer(group_id)
        if not batch:
            return

        self.tracker_manager.set_active_thinking(group_id, True)
        try:
            res = await handle_proactive_batch(self, event, batch)
            if res:
                event.is_at_or_wake_command = True
                event.set_extra("chat_echo_triggered", True)
                event.set_extra("chat_echo_mode", "proactive")
                event.set_extra(
                    "selected_provider", self.config_helper.generator_provider()
                )
        finally:
            self.tracker_manager.set_active_thinking(group_id, False)

    # ======== Web API handlers ========

    async def page_token_stats(self):
        try:
            await self.token_counter.flush_all()
            from quart import jsonify
            from quart import request as qreq

            period = qreq.args.get("period", "all") if qreq else "all"
            global_total = await self.token_counter.get_global_total(period)
            groups = await self.token_counter.get_all_groups_summary(period)
            return jsonify(
                {"status": "ok", "data": {"global": global_total, "groups": groups}}
            )
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to get token stats: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def page_token_history(self):
        try:
            await self.token_counter.flush_all()
            from quart import jsonify
            from quart import request as qreq

            days = int(qreq.args.get("days", 30)) if qreq else 30
            groups_data = await self.token_counter.get_all_groups_daily(min(days, 365))
            return jsonify({"status": "ok", "data": {"groups": groups_data}})
        except Exception as e:
            from quart import jsonify

            return jsonify({"status": "error", "message": str(e)})

    async def get_image_caption(
        self, image_url: str, umo: str, force: bool = False
    ) -> str:
        """Call LLM provider to get description/caption for a given image URL."""
        # Query cache first
        img_hash = await self.caption_cache.get_hash(image_url)
        cached = self.caption_cache.get(img_hash)
        if cached:
            self.logger.info(
                f"[ImageCache] Hit cache for image {image_url[:60]}... -> {cached[:30]}"
            )
            return cached

        # Check probability for new image captioning
        if not force and not is_probability_hit(
            self.config_helper.image_caption_probability()
        ):
            self.logger.info(
                f"[ImageCache] Cache miss for image {image_url[:60]}..., but skipped captioning due to probability constraint ({self.config_helper.image_caption_probability()}%)."
            )
            return ""

        provider_id = self.config_helper.image_caption_provider()
        global_cfg = self.context.get_config(umo=umo)

        # Fallback to global default image caption provider if not set in plugin
        if not provider_id:
            provider_id = global_cfg.get("provider_settings", {}).get(
                "default_image_caption_provider_id", ""
            )

        if not provider_id:
            self.logger.warning(
                "No image caption provider configured in plugin or global settings."
            )
            return ""

        prov = self.context.get_provider_by_id(provider_id)
        if prov is None:
            self.logger.error(f"Image caption provider '{provider_id}' not found.")
            return ""

        prompt = global_cfg.get("provider_settings", {}).get(
            "image_caption_prompt", "Please describe the image using Chinese."
        )

        compressed_url = image_url
        is_temp_file = False
        try:
            import os

            from .helpers import compress_image_if_needed

            compressed_url = await compress_image_if_needed(image_url)
            if image_url.startswith("http://") or image_url.startswith("https://"):
                is_temp_file = True
            elif compressed_url != image_url:
                is_temp_file = True

            self.logger.debug(
                f"Requesting image caption from provider {provider_id} for URL {compressed_url}"
            )
            resp = await prov.text_chat(prompt=prompt, image_urls=[compressed_url])
            if resp and resp.completion_text:
                caption = resp.completion_text.strip()
                self.caption_cache.set(img_hash, caption, image_url=image_url)
                return caption
        except Exception as e:
            self.logger.exception(f"Failed to get image caption: {e}")
        finally:
            if is_temp_file and compressed_url and os.path.exists(compressed_url):
                try:
                    os.unlink(compressed_url)
                except Exception:
                    pass

        return ""

    async def api_caption_cache_list(self):
        """GET handler: paginated caption cache list with optional search."""
        try:
            from quart import jsonify
            from quart import request as qreq

            offset = int(qreq.args.get("offset", 0)) if qreq else 0
            limit = int(qreq.args.get("limit", 20)) if qreq else 20
            search = qreq.args.get("search", "").strip() if qreq else ""
            limit = min(limit, 100)
            items = self.caption_cache.get_all(offset, limit, search=search)
            total = self.caption_cache.get_count(search=search)
            return jsonify(
                {
                    "status": "ok",
                    "data": {
                        "items": items,
                        "total": total,
                        "offset": offset,
                        "limit": limit,
                        "search": search,
                    },
                }
            )
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to list caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_stats(self):
        """GET handler: caption cache statistics."""
        try:
            from quart import jsonify

            count = self.caption_cache.get_count()
            db_size = self.caption_cache.get_db_size()
            return jsonify(
                {"status": "ok", "data": {"count": count, "db_size": db_size}}
            )
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to get caption cache stats: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_delete(self):
        """POST handler: delete a single cache entry."""
        try:
            from quart import jsonify
            from quart import request as qreq

            body = await qreq.get_json()
            img_hash = body.get("img_hash", "") if body else ""
            if not img_hash:
                return jsonify({"status": "error", "message": "img_hash is required"})
            ok = self.caption_cache.delete(img_hash)
            return jsonify({"status": "ok", "deleted": ok})
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to delete caption cache entry: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_clear(self):
        """POST handler: clear all cache entries."""
        try:
            from quart import jsonify

            deleted = self.caption_cache.clear()
            self.logger.info(
                f"[CaptionCache] Cleared all entries, deleted {deleted} items."
            )
            return jsonify({"status": "ok", "deleted": deleted})
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to clear caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_clear_before(self):
        """POST handler: clear cache entries before a given timestamp."""
        try:
            from quart import jsonify
            from quart import request as qreq

            body = await qreq.get_json()
            before = float(body.get("before", 0)) if body else 0
            if before <= 0:
                return jsonify(
                    {
                        "status": "error",
                        "message": "valid 'before' timestamp is required",
                    }
                )
            deleted = self.caption_cache.delete_before(before)
            self.logger.info(
                f"[CaptionCache] Cleared entries before {before}, deleted {deleted} items."
            )
            return jsonify({"status": "ok", "deleted": deleted})
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to clear old caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_update(self):
        """POST handler: update caption text for a cache entry."""
        try:
            from quart import jsonify
            from quart import request as qreq

            body = await qreq.get_json()
            img_hash = body.get("img_hash", "") if body else ""
            caption = body.get("caption", "") if body else ""
            if not img_hash:
                return jsonify({"status": "error", "message": "img_hash is required"})
            ok = self.caption_cache.update_caption(img_hash, caption)
            return jsonify({"status": "ok", "updated": ok})
        except Exception as e:
            from quart import jsonify

            self.logger.exception(f"Failed to update caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    # ======== Human-like Mode ========

    async def _ensure_schedule(self, group_id: str, umo: str, personality) -> None:
        """Ensure a schedule exists for this group, refresh when exhausted."""
        schedule = self.tracker_manager.get_schedule(group_id)
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
            self.tracker_manager.cancel_schedule_timer(group_id)
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
            provider_id = self.config_helper.analyzer_provider()
            if not provider_id and umo:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(umo)
                except Exception:
                    pass
            if not provider_id:
                return
            resp = await self.context.llm_generate(
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
                import re

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
            self.tracker_manager.set_schedule(group_id, schedule)
            self.tracker_manager.cancel_schedule_timer(group_id)
            self._apply_schedule(group_id, schedule, now_dt)
            self.logger.info(
                f"[HumanMode] {group_id} schedule refreshed: {len(schedule)} items"
            )
        except Exception as e:
            self.logger.exception(f"[HumanMode] Failed to generate schedule: {e}")

    def _apply_schedule(self, group_id: str, schedule: list, now_dt: datetime) -> None:
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
        current_state = self.tracker_manager.get_state(group_id)
        if current_state.get("manual"):
            self.tracker_manager.set_state(group_id, {**current_state, "manual": False})
        elif current_match:
            self.tracker_manager.set_state(
                group_id,
                {
                    "name": current_match.get("state", "空闲"),
                    "activity": float(current_match.get("activity", 1.0)),
                    "reason": current_match.get("reason", ""),
                },
            )
            self.logger.info(
                f"[HumanMode] {group_id} state: {current_match.get('state')} (activity={current_match.get('activity')})"
            )
        # Schedule next transition
        if next_item:
            try:
                h, m = map(int, next_item["until"].split(":"))
                target = datetime(today.year, today.month, today.day, h, m)
                if target <= now_dt:
                    from datetime import timedelta

                    target += timedelta(days=1)
                delay = (target - datetime.now()).total_seconds()
                if delay > 0:

                    async def _transition():
                        await asyncio.sleep(delay)
                        dt = datetime.now()
                        self._apply_schedule(group_id, schedule, dt)

                    task = asyncio.create_task(_transition())
                    self.tracker_manager.set_schedule_timer(group_id, task)
            except (ValueError, AttributeError):
                pass

    async def terminate(self):
        self.logger.info("主动接话插件卸载中...")
        await self.token_counter.stop()
        self.tracker_manager.clear_all()
