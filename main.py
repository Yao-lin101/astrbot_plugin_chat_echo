"""
AstrBot 主动接话插件 (astrbot_plugin_chat_echo)

Refactored for improved maintainability.
"""

import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools, register

from .config import ConfigHelper, upgrade_config
from .handlers import handle_proactive, handle_reply, start_tracking
from .helpers import (
    extract_bot_text,
    extract_image_urls,
    extract_sent_text,
    is_group_event,
    is_probability_hit,
)
from .llm_client import LLMHandler
from .tracker import TrackerManager
from .utils.token_counter import TokenCounter

PLUGIN_NAME = "astrbot_plugin_chat_echo"
PROACTIVE_WINDOW_SIZE = 10


@register("astrbot_plugin_chat_echo", "AMYdd00", "主动接话插件", "1.0.4")
class EchoPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger

        data_dir = StarTools.get_data_dir("chat_echo")
        self.token_counter = TokenCounter(data_dir)

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

    async def initialize(self):
        self.logger.info("主动接话插件初始化完成")
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
        bot_text = extract_bot_text(response)
        if self.config_helper.trigger_mode() in ("llm_response", "any_message"):
            await start_tracking(self, event, bot_text)

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

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        """Listen to all group messages to collect replies or initiate proactive participation."""
        if not is_group_event(event):
            return

        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self.config_helper.is_group_allowed(group_id, umo):
            return

        now = time.time()

        is_bot = False
        try:
            self_id = event.get_self_id()
            sender_id = event.get_sender_id()
            if self_id and sender_id and str(sender_id) == str(self_id):
                is_bot = True
        except (AttributeError, TypeError, ValueError):
            pass

        msg_content = event.message_str or ""
        if not msg_content.strip():
            msg_content = event.get_message_outline()

        image_urls = await extract_image_urls(event)
        if image_urls and self.config_helper.enable_image_caption():
            captions = []
            for url in image_urls:
                caption = await self.get_image_caption(url, umo)
                if caption:
                    captions.append(caption)
            if captions:
                msg_content += " " + " ".join(f"[图片描述: {cap}]" for cap in captions)

        msg = {
            "user_name": event.get_sender_name(),
            "user_id": str(event.get_sender_id()),
            "content": msg_content,
            "image_urls": image_urls,
            "time": now,
        }

        window = self.tracker_manager.add_to_recent(
            group_id, msg, PROACTIVE_WINDOW_SIZE
        )

        if is_bot:
            return

        # ====== Reply Mode (Route 1) ======
        tracker = self.tracker_manager.get_tracker(group_id)
        if tracker and tracker.alive:
            if now > tracker.expire_at:
                self.tracker_manager.cleanup_tracker(group_id)
            else:
                tracker.expire_at = now + self.config_helper.track_timeout()
                tracker.collected.append(msg)
                if tracker.analyzing or self.tracker_manager.is_active_thinking(
                    group_id
                ):
                    return
                if is_probability_hit(
                    self.config_helper.get_effective_reply_prob(group_id, umo)
                ):
                    tracker.analyzing = True
                    return await handle_reply(self, tracker, event)
                return

        # ====== Proactive Mode (Route 2) ======
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
        if is_probability_hit(active_prob):
            self.tracker_manager.set_active_thinking(group_id, True)
            return await handle_proactive(self, event, msg, window)

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

    async def get_image_caption(self, image_url: str, umo: str) -> str:
        """Call LLM provider to get description/caption for a given image URL."""
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

        try:
            self.logger.debug(
                f"Requesting image caption from provider {provider_id} for URL {image_url}"
            )
            resp = await prov.text_chat(prompt=prompt, image_urls=[image_url])
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception as e:
            self.logger.exception(f"Failed to get image caption: {e}")

        return ""

    async def terminate(self):
        self.logger.info("主动接话插件卸载中...")
        await self.token_counter.stop()
        self.tracker_manager.clear_all()
