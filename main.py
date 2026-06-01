"""
AstrBot 主动接话插件 (astrbot_plugin_chat_echo)

Refactored for improved maintainability.
"""

import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

from .config import ConfigHelper, upgrade_config
from .handlers import process_group_message, start_tracking
from .helpers import extract_bot_text, extract_sent_text, is_group_event
from .llm_client import LLMHandler
from .tracker import TrackerManager
from .utils.caption_cache import ImageCaptionCache
from .utils.token_counter import TokenCounter


@register("astrbot_plugin_chat_echo", "AMYdd00, Yao-lin101", "主动接话插件", "1.2.0")
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

        from .web.web_api import EchoWebApi

        self.web_api = EchoWebApi(self)
        self.web_api.register_routes()

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
        await process_group_message(self, event)

    async def _ensure_schedule(self, group_id: str, umo: str, personality) -> None:
        from .services.human_mode import ensure_schedule

        await ensure_schedule(self, group_id, umo, personality)

    def _apply_schedule(self, group_id: str, schedule: list, now_dt: datetime) -> None:
        from .services.human_mode import apply_schedule

        apply_schedule(self, group_id, schedule, now_dt)

    async def get_image_caption(
        self, image_url: str, umo: str, force: bool = False
    ) -> str:
        from .services.image_caption import get_image_caption

        return await get_image_caption(self, image_url, umo, force)

    async def terminate(self):
        self.logger.info("主动接话插件卸载中...")
        await self.token_counter.stop()
        self.tracker_manager.clear_all()
