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
                content = bot_text
                image_urls = []
                if not content:
                    try:
                        result = event.get_result()
                        if result and hasattr(result, "chain") and result.chain:
                            from astrbot.api.message_components import (
                                Image as ImageComponent,
                            )

                            for comp in result.chain:
                                if isinstance(comp, ImageComponent):
                                    url = comp.url or (
                                        comp.file
                                        if comp.file and comp.file.startswith("http")
                                        else None
                                    )
                                    if url:
                                        image_urls.append(url)
                    except Exception:
                        pass
                    captions = []
                    for url in image_urls:
                        img_hash = await self.caption_cache.get_hash(url)
                        cached = self.caption_cache.get(img_hash)
                        if cached:
                            captions.append(f"[图片描述: {cached}]")
                    content = " ".join(captions) if captions else "[图片/表情]"

                if not tracker.bot_message_sent:
                    tracker.bot_message = content
                await self.append_bot_message_to_context(umo, content)
                tracker.last_llm_text = content
                tracker.last_llm_time = time.time()
                tracker.detection_count = 0
                tracker.expire_at = time.time() + self.config_helper.track_timeout()
            else:
                content = bot_text
                image_urls = []
                if not content:
                    try:
                        result = event.get_result()
                        if result and hasattr(result, "chain") and result.chain:
                            from astrbot.api.message_components import (
                                Image as ImageComponent,
                            )

                            for comp in result.chain:
                                if isinstance(comp, ImageComponent):
                                    url = comp.url or (
                                        comp.file
                                        if comp.file and comp.file.startswith("http")
                                        else None
                                    )
                                    if url:
                                        image_urls.append(url)
                    except Exception:
                        pass
                    captions = []
                    for url in image_urls:
                        img_hash = await self.caption_cache.get_hash(url)
                        cached = self.caption_cache.get(img_hash)
                        if cached:
                            captions.append(f"[图片描述: {cached}]")
                    content = " ".join(captions) if captions else "[图片/表情]"
                await start_tracking(self, event, content)
                await self.append_bot_message_to_context(umo, content)
                tracker = self.tracker_manager.get_tracker(group_id)
                if tracker:
                    tracker.last_llm_text = content
                    tracker.last_llm_time = time.time()
            return

        if self.config_helper.trigger_mode() in ("llm_response", "any_message"):
            content = bot_text
            image_urls = []
            if not content:
                try:
                    result = event.get_result()
                    if result and hasattr(result, "chain") and result.chain:
                        from astrbot.api.message_components import (
                            Image as ImageComponent,
                        )

                        for comp in result.chain:
                            if isinstance(comp, ImageComponent):
                                url = comp.url or (
                                    comp.file
                                    if comp.file and comp.file.startswith("http")
                                    else None
                                )
                                if url:
                                    image_urls.append(url)
                except Exception:
                    pass
                captions = []
                for url in image_urls:
                    img_hash = await self.caption_cache.get_hash(url)
                    cached = self.caption_cache.get(img_hash)
                    if cached:
                        captions.append(f"[图片描述: {cached}]")
                content = " ".join(captions) if captions else "[图片/表情]"
            await start_tracking(self, event, content)
            await self.append_bot_message_to_context(umo, content)
            tracker = self.tracker_manager.get_tracker(group_id)
            if tracker:
                tracker.last_llm_text = content
                tracker.last_llm_time = time.time()

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Inject tracked conversation history before LLM request is sent."""
        if not event.get_extra("chat_echo_triggered"):
            return

        umo = event.unified_msg_origin
        injected_contexts = []
        gcc = self.get_group_chat_context()
        if gcc:
            record_id = event.get_extra("_group_context_record_id", None)
            prompt_idx = event.get_extra("_group_context_raw_idx", -1)
            
            from collections import deque
            lock = gcc._get_lock(umo)
            async with lock:
                records = gcc.raw_records.get(umo)
                if records:
                    raw_list = list(records)
                    id_list = list(gcc._record_ids.get(umo, deque()))
                    if isinstance(record_id, str) and record_id in id_list:
                        prompt_idx = id_list.index(record_id)
                    
                    if 0 <= prompt_idx < len(raw_list):
                        records_to_inject = raw_list[:prompt_idx]
                        remaining = raw_list[prompt_idx + 1 :]
                        remaining_ids = id_list[prompt_idx + 1 :] if id_list else []
                        records.clear()
                        records.extend(remaining)
                        if id_list:
                            record_ids = gcc._record_ids[umo]
                            record_ids.clear()
                            record_ids.extend(remaining_ids)
                        
                        for record in records_to_inject:
                            if "]: " in record:
                                header, content = record.split("]: ", 1)
                                header = header[1:]
                                if "/" in header:
                                    nickname, _ = header.rsplit("/", 1)
                                else:
                                    nickname = header
                            else:
                                nickname = "Unknown"
                                content = record

                            role = "assistant" if nickname == "你" else "user"
                            if role == "user":
                                content = f"{nickname}: {content}"
                            
                            injected_contexts.append(
                                {"role": role, "content": content, "_no_save": True}
                            )

        if injected_contexts:
            event.set_extra("chat_echo_original_contexts", req.contexts)
            req.contexts = injected_contexts
            self.logger.debug(
                f"[ChatEcho] Overwrote LLM contexts with {len(injected_contexts)} tracked group messages from native GroupChatContext."
            )

        event.set_extra("_group_context_record_id", None)
        event.set_extra("_group_context_raw_idx", -1)

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

        bot_text = extract_sent_text(event)
        image_urls = []
        try:
            result = event.get_result()
            if result and hasattr(result, "chain") and result.chain:
                from astrbot.api.message_components import Image as ImageComponent

                for comp in result.chain:
                    if isinstance(comp, ImageComponent):
                        url = comp.url or (
                            comp.file
                            if comp.file and comp.file.startswith("http")
                            else None
                        )
                        if url:
                            image_urls.append(url)
        except Exception:
            pass

        content = bot_text
        if not content:
            captions = []
            for url in image_urls:
                img_hash = await self.caption_cache.get_hash(url)
                cached = self.caption_cache.get(img_hash)
                if cached:
                    captions.append(f"[图片描述: {cached}]")
            content = " ".join(captions) if captions else "[图片/表情]"

        tracker = self.tracker_manager.get_tracker(group_id)
        if tracker and tracker.alive:
            # Check if this sent message content matches the whole or a split segment of the last LLM response text
            # within a 5-second window. If so, skip it as it's already recorded in full by on_llm_response.
            is_llm_part = False
            if tracker.last_llm_text and (time.time() - tracker.last_llm_time < 5.0):
                clean_content = content.strip()
                if clean_content:
                    llm_parts = [
                        p.strip()
                        for p in tracker.last_llm_text.split("\n")
                        if p.strip()
                    ]
                    if (
                        clean_content == tracker.last_llm_text.strip()
                        or clean_content in llm_parts
                    ):
                        is_llm_part = True
                else:
                    is_llm_part = True

            if is_llm_part:
                tracker.bot_message_sent = True
                return

            if not tracker.bot_message_sent:
                tracker.bot_message_sent = True
                tracker.bot_message = content
                await self.append_bot_message_to_context(umo, content)
                return

            await self.append_bot_message_to_context(umo, content)
            tracker.detection_count = 0
            tracker.expire_at = time.time() + self.config_helper.track_timeout()
        else:
            if (
                self.tracker_manager.is_active_thinking(group_id)
                or self.config_helper.trigger_mode() == "any_message"
            ):
                self.tracker_manager.set_active_thinking(group_id, False)
                await start_tracking(self, event, content)
                await self.append_bot_message_to_context(umo, content)
                new_tracker = self.tracker_manager.get_tracker(group_id)
                if new_tracker:
                    new_tracker.bot_message_sent = True

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

    def get_group_chat_context(self):
        """Retrieve the GroupChatContext instance from the registered stars."""
        from astrbot.core.star.star import star_map
        for star_meta in star_map.values():
            if star_meta.star_cls and hasattr(star_meta.star_cls, "group_chat_context"):
                return star_meta.star_cls.group_chat_context
        return None

    async def append_bot_message_to_context(self, umo: str, content: str):
        """Safely append a bot message to the native GroupChatContext."""
        gcc = self.get_group_chat_context()
        if not gcc:
            return
        import uuid
        from collections import deque
        datetime_str = datetime.now().strftime("%H:%M:%S")
        final_message = f"[你/{datetime_str}]: {content}"
        self.tracker_manager.add_to_history(umo, final_message)
        lock = gcc._get_lock(umo)
        async with lock:
            records = gcc.raw_records[umo]
            record_ids = gcc._record_ids[umo]
            records.append(final_message)
            record_ids.append(uuid.uuid4().hex)
            while len(records) > 300:
                records.popleft()
                if record_ids:
                    record_ids.popleft()

    async def terminate(self):
        self.logger.info("主动接话插件卸载中...")
        await self.token_counter.stop()
        self.tracker_manager.clear_all()
