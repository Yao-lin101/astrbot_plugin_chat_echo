"""
AstrBot 主动接话插件 (astrbot_plugin_chat_echo)

功能：
Bot 发出消息后，自动监听后续群友的回复，使用 LLM 分析是否在跟 Bot 说话，
如果是，则使用 LLM 生成自然回复并主动发言。

双模式：
- 回复模式（Route 1）：Bot 发言后跟踪窗口内群友消息，按概率分析是否回复 Bot
- 主动模式（Route 2）：Bot 没发言时，按概率随机抽取消息分析话题并主动参与讨论

配置项详见 _conf_schema.json
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.core.message.components import Image as ImageComponent
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.agent.message import UserMessageSegment, AssistantMessageSegment, TextPart
from astrbot.core.agent.tool import ToolSet

from .utils.token_counter import TokenCounter

PLUGIN_NAME = "astrbot_plugin_chat_echo"

DEFAULT_ANALYZER_PROMPT = """你是一个群聊对话分析师。请分析以下群聊上下文，判断群友的最新消息是否在和Bot对话。

判断标准：
1. Bot上一条/上几条消息是Bot发的，群友紧接着Bot发言 → 默认是在对话
2. 消息中提到Bot的名字或@了Bot → 肯定是在对话
3. 群友在跟别人说话（叫了别人名字、回应非Bot的消息）→ 不是在对话
4. 群友只是发纯语气词/表情且无任何上下文关联 → 不是在对话

核心逻辑：Bot刚说完话就有人接话，正常聊天中就是对方在回应Bot。
宁可误判也不要漏判。

请用以下JSON格式回复（不要包含其他内容）：
{
  "is_reply_to_bot": "yes|no",
  "reason": "简短的原因说明"
}"""

DEFAULT_GENERATOR_PROMPT = """你正在参与群聊讨论，请自然地接上对话。记住你是群里的一个成员，不是AI助手。

【回复要求】
- 回复要简短，一两句话就行，不要长篇大论
- 口语化，像真人聊天那样
- 结合Bot刚才说的内容和群友的回复来回应
- 可以适当使用工具、搜索、发图等来丰富回复
- 不要输出思考过程
- 不要长篇说教或分析"""

DEFAULT_PROACTIVE_ANALYZER_PROMPT = """你是一个群聊观察者。请分析以下群聊内容，判断Bot是否应该参与讨论。

判断标准：
1. 群消息中提到的话题Bot是否了解或有见解
2. 对话是否在邀请更多人参与（提问、征求意见等）
3. Bot是否有想说的内容（吐槽、补充、提问、分享等）
4. 只要是Bot有内容想回复，就回复"yes"
5. 只有Bot完全无话可说时才回复"no"

请用以下JSON格式回复（不要包含其他内容）：
{
  "should_join": "yes|no",
  "reason": "简短的原因说明"
}"""

CONFIG_VERSION = 3

MAX_CONTEXT_MESSAGES = 20
PROACTIVE_WINDOW_SIZE = 10

# 配置版本文件（独立于 _conf_schema.json，避免污染配置）
CONFIG_VERSION_FILE = "_config_version.txt"


def _extract_image_urls(event: AstrMessageEvent) -> list:
    """从事件消息中提取图片 URL 列表"""
    urls = []
    try:
        for comp in event.get_messages():
            if isinstance(comp, ImageComponent):
                if comp.url:
                    urls.append(comp.url)
                elif comp.file and (comp.file.startswith("http://") or comp.file.startswith("https://")):
                    urls.append(comp.file)
    except Exception:
        pass
    return urls


class ConversationTracker:
    __slots__ = (
        "group_id", "unified_msg_origin", "bot_message",
        "trigger_user_name", "trigger_user_id", "trigger_message",
        "collected", "expire_at",
        "analyzing", "detection_count", "alive", "group_name",
    )

    def __init__(self, group_id: str, unified_msg_origin: str,
                 bot_message: str, trigger_user_name: str,
                 trigger_user_id: str, trigger_message: str,
                 expire_seconds: int):
        self.group_id = group_id
        self.unified_msg_origin = unified_msg_origin
        self.bot_message = bot_message
        self.trigger_user_name = trigger_user_name
        self.trigger_user_id = trigger_user_id
        self.trigger_message = trigger_message
        self.collected: List[dict] = []
        self.expire_at = time.time() + expire_seconds
        self.analyzing = False
        self.detection_count = 0
        self.alive = True
        self.group_name = ""


@register("astrbot_plugin_chat_echo", "AMYdd00", "主动接话插件", "1.0.3")
class EchoPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger
        self._trackers: Dict[str, ConversationTracker] = {}
        self._cooldowns: Dict[str, float] = {}
        self._proactive_flag: Dict[str, bool] = {}
        self._active_thinking: Dict[str, bool] = {}
        self._active_cooldowns: Dict[str, float] = {}
        # 主动模式轮数追踪（只影响 Route 2，不影响 @bot 对话）
        self._proactive_rounds: Dict[str, int] = {}

        self._parsed_groups: List[Tuple[str, Optional[int], Optional[int]]] = []
        self._recent_messages: Dict[str, List[dict]] = {}

        data_dir = StarTools.get_data_dir('chat_echo')
        self.token_counter = TokenCounter(data_dir)

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
        self._preparse_group_config()
        self._upgrade_config()
        self.token_counter.start()

    def _get_config_version_file(self):
        """获取配置版本文件路径（数据目录下，独立于 _conf_schema.json）"""
        return Path(StarTools.get_data_dir('chat_echo')) / CONFIG_VERSION_FILE

    def _upgrade_config(self):
        try:
            ver_file = self._get_config_version_file()
            cfg_version = 0
            if ver_file.exists():
                cfg_version = int(ver_file.read_text().strip())
            if cfg_version >= CONFIG_VERSION:
                return
            self.logger.info(f"检测到配置版本 {cfg_version}，正在升级至 {CONFIG_VERSION}，将覆盖提示词...")
            if isinstance(self.config, dict):
                self.config["proactive_analyzer_system_prompt"] = DEFAULT_PROACTIVE_ANALYZER_PROMPT
                self.config["analyzer_system_prompt"] = DEFAULT_ANALYZER_PROMPT
                self.config["generator_system_prompt"] = DEFAULT_GENERATOR_PROMPT
            else:
                self.config.proactive_analyzer_system_prompt = DEFAULT_PROACTIVE_ANALYZER_PROMPT
                self.config.analyzer_system_prompt = DEFAULT_ANALYZER_PROMPT
                self.config.generator_system_prompt = DEFAULT_GENERATOR_PROMPT
            self.config.save_config()
            ver_file.parent.mkdir(parents=True, exist_ok=True)
            ver_file.write_text(str(CONFIG_VERSION))
            self.logger.info(f"配置已升级并保存 (v{CONFIG_VERSION})")
        except Exception as e:
            self.logger.exception(f"配置升级失败: {e}")

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _trigger_mode(self) -> str:
        return str(self._cfg("trigger_mode", "llm_response"))

    def _track_timeout(self) -> int:
        return int(self._cfg("track_timeout_seconds", 120))

    def _max_detection_count(self) -> int:
        return int(self._cfg("max_detection_count", 10))

    def _global_probability(self) -> int:
        return int(self._cfg("reply_probability", 100))

    def _active_probability(self) -> int:
        return int(self._cfg("active_probability", 0))

    def _enabled_groups(self) -> list:
        return self._cfg("enabled_groups", [])

    def _max_rounds(self) -> int:
        return int(self._cfg("max_proactive_rounds", 3))

    def _proactive_cooldown(self) -> int:
        return int(self._cfg("proactive_cooldown_seconds", 300))

    def _analyzer_provider(self) -> str:
        return str(self._cfg("analyzer_provider_id", "") or "")

    def _generator_provider(self) -> str:
        return str(self._cfg("generator_provider_id", "") or "")

    def _enable_llm_tools(self) -> bool:
        return bool(self._cfg("enable_llm_tools", True))

    def _analyzer_prompt(self) -> str:
        raw = self._cfg("analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_ANALYZER_PROMPT

    def _generator_prompt(self) -> str:
        raw = self._cfg("generator_system_prompt", "")
        return raw.strip() or DEFAULT_GENERATOR_PROMPT

    def _proactive_analyzer_prompt(self) -> str:
        raw = self._cfg("proactive_analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_PROACTIVE_ANALYZER_PROMPT

    @staticmethod
    def _parse_group_entry(entry: str) -> Tuple[str, Optional[int], Optional[int]]:
        entry = entry.strip()
        if not entry:
            return "", None, None
        parts = entry.split(":")
        if len(parts) == 3:
            return parts[0], int(parts[1]) if parts[1] else None, int(parts[2]) if parts[2] else None
        elif len(parts) == 2:
            return parts[0], int(parts[1]) if parts[1].isdigit() else None, None
        else:
            return entry, None, None

    def _preparse_group_config(self):
        enabled = self._enabled_groups()
        self._parsed_groups = [
            self._parse_group_entry(entry) for entry in enabled if entry.strip()
        ]

    def _is_match_group(self, gid: str, group_id: str, umo: str) -> bool:
        if gid == group_id or gid == umo:
            return True
        try:
            if gid == umo.rsplit(":", 1)[-1]:
                return True
        except (AttributeError, IndexError, ValueError):
            pass
        return False

    def _is_group_allowed(self, group_id: str, umo: str) -> bool:
        if not self._parsed_groups:
            return True
        for gid, _, _ in self._parsed_groups:
            if self._is_match_group(gid, group_id, umo):
                return True
        return False

    def _get_group_probability(self, group_id: str, umo: str) -> Tuple[Optional[int], Optional[int]]:
        if not self._parsed_groups:
            return None, None
        for gid, reply_p, active_p in self._parsed_groups:
            if self._is_match_group(gid, group_id, umo):
                return reply_p, active_p
        return None, None

    @staticmethod
    def _is_group_event(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except (AttributeError, TypeError):
            return False

    @staticmethod
    def _extract_bot_text(response) -> str:
        if hasattr(response, "completion_text"):
            return response.completion_text or ""
        if hasattr(response, "text"):
            return response.text or ""
        if isinstance(response, str):
            return response
        return ""

    @staticmethod
    def _extract_sent_text(event: AstrMessageEvent) -> str:
        bot_text = ""
        try:
            result = event.get_result()
            if result and hasattr(result, "chain") and result.chain:
                for comp in result.chain:
                    if hasattr(comp, "text"):
                        bot_text += comp.text or ""
                    elif hasattr(comp, "content"):
                        bot_text += comp.content or ""
        except Exception:
            pass
        return bot_text

    async def _start_tracking(self, event: AstrMessageEvent, bot_message: str = ""):
        group_id = str(event.get_group_id())
        unified_msg_origin = event.unified_msg_origin
        sender_name = event.get_sender_name()
        sender_id = str(event.get_sender_id())
        trigger_message = event.message_str
        if group_id in self._trackers and self._trackers[group_id].alive:
            return
        gname = ""
        try:
            g = await event.get_group()
            gname = g.group_name if g else ""
        except Exception as e:
            self.logger.exception(f"获取群名失败: {e}")
        tracker = ConversationTracker(
            group_id=group_id, unified_msg_origin=unified_msg_origin,
            bot_message=bot_message, trigger_user_name=sender_name,
            trigger_user_id=sender_id, trigger_message=trigger_message,
            expire_seconds=self._track_timeout(),
        )
        if gname:
            tracker.group_name = gname
        self._trackers[group_id] = tracker
        if gname:
            self.token_counter.set_group_name(group_id, gname)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """Bot LLM 回复后触发，开始跟踪群友后续回复"""
        if not self._is_group_event(event):
            return
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self._is_group_allowed(group_id, umo):
            return
        if self._proactive_flag.get(group_id):
            return
        bot_text = self._extract_bot_text(response)
        if self._trigger_mode() in ("llm_response", "any_message"):
            await self._start_tracking(event, bot_text)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """Bot 发送消息后触发，开始跟踪群友后续回复"""
        if not self._is_group_event(event):
            return
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self._is_group_allowed(group_id, umo):
            return
        if self._proactive_flag.get(group_id):
            return
        if self._trigger_mode() != "any_message":
            return
        bot_text = self._extract_sent_text(event)
        await self._start_tracking(event, bot_text)

    @staticmethod
    def _is_probability_hit(prob: int) -> bool:
        return prob >= 100 or random.randint(1, 100) <= prob

    def _get_effective_reply_prob(self, group_id: str, umo: str) -> int:
        gp, _ = self._get_group_probability(group_id, umo)
        if gp is not None:
            return gp
        return self._global_probability()

    def _get_effective_active_prob(self, group_id: str, umo: str) -> int:
        _, ap = self._get_group_probability(group_id, umo)
        if ap is not None:
            return ap
        return self._active_probability()

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息，收集回复并判断是否需要主动接话或参与讨论"""
        if not self._is_group_event(event):
            return

        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if not self._is_group_allowed(group_id, umo):
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

        image_urls = _extract_image_urls(event)

        msg = {
            "user_name": event.get_sender_name(),
            "user_id": str(event.get_sender_id()),
            "content": msg_content,
            "image_urls": image_urls,
            "time": now,
        }

        if group_id not in self._recent_messages:
            self._recent_messages[group_id] = []
        window = self._recent_messages[group_id]
        window.append(msg)
        if len(window) > PROACTIVE_WINDOW_SIZE:
            window.pop(0)

        if is_bot:
            return

        # ====== 回复模式 (Route 1)：Bot 发言后的跟踪窗口 ======
        # @bot 对话不受 max_proactive_rounds 和 proactive_cooldown 限制
        tracker = self._trackers.get(group_id)
        if tracker and tracker.alive:
            if now > tracker.expire_at:
                self._cleanup_tracker(group_id)
            else:
                tracker.expire_at = now + self._track_timeout()
                tracker.collected.append(msg)
                if tracker.analyzing or self._active_thinking.get(group_id):
                    return
                if self._is_probability_hit(self._get_effective_reply_prob(group_id, umo)):
                    tracker.analyzing = True
                    return await self._handle_reply(tracker, event)
                return

        # ====== 主动模式 (Route 2)：Bot 随机参与讨论 ======
        if self._active_thinking.get(group_id) or self._proactive_flag.get(group_id):
            return
        active_prob = self._get_effective_active_prob(group_id, umo)
        if active_prob <= 0:
            return
        last_active = self._active_cooldowns.get(group_id, 0)
        if now - last_active < self._proactive_cooldown():
            return
        rounds = self._proactive_rounds.get(group_id, 0)
        if rounds >= self._max_rounds():
            return
        tracker_check = self._trackers.get(group_id)
        if tracker_check and tracker_check.alive:
            return
        if self._is_probability_hit(active_prob):
            self._active_thinking[group_id] = True
            return await self._handle_proactive(event, msg, window)

    async def _handle_reply(self, tracker: ConversationTracker, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        """处理回复模式。@bot 后的正常回复对话，不受轮数限制。"""
        group_id = tracker.group_id
        try:
            context_text, image_urls = self._build_analyze_context(tracker)
            self.logger.info(f"[回复] 分析群 {group_id} 的回复是否针对 Bot...")
            analysis = await self._call_analyzer(context_text, image_urls=image_urls, umo=tracker.unified_msg_origin)
            if analysis is None:
                return None
            is_reply = analysis.get("is_reply_to_bot", "no")
            reason = analysis.get("reason", "")
            if is_reply == "no":
                tracker.detection_count += 1
                max_detect = self._max_detection_count()
                self.logger.info(f"[回复] 群 {group_id} 不针对 Bot ({reason}) | {tracker.detection_count}/{max_detect}")
                if tracker.detection_count >= max_detect:
                    self.logger.info(f"[回复] 群 {group_id} 已达最大检测次数，停止")
                    self._cleanup_tracker(group_id)
                return None
            self.logger.info(f"[回复] 群 {group_id} 的回复针对 Bot | 原因: {reason}")
            if self._enable_llm_tools():
                reply_text = await self._call_generator_with_tools(context_text, event=event, image_urls=image_urls, umo=tracker.unified_msg_origin)
            else:
                reply_text = await self._call_generator_raw(context_text, image_urls=image_urls, umo=tracker.unified_msg_origin)
            if not reply_text:
                self.logger.warning(f"[回复] 群 {group_id} 生成回复为空")
                return None
            self.logger.info(f"[回复] 回复群 {group_id}: {reply_text[:60]}")
            self._proactive_flag[group_id] = True

            result = MessageEventResult()
            result.message(reply_text)
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            try:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(tracker.unified_msg_origin)
                if cid:
                    await conv_mgr.add_message_pair(
                        cid=cid,
                        user_message=UserMessageSegment(content=[TextPart(text=tracker.trigger_message)]),
                        assistant_message=AssistantMessageSegment(content=[TextPart(text=reply_text)]),
                    )
            except Exception as e:
                self.logger.exception(f"[回复] 写入会话历史失败: {e}")
            tracker.detection_count = 0
            tracker.expire_at = time.time() + self._track_timeout()
            self._proactive_flag[group_id] = False
            return result

        except Exception as e:
            self.logger.exception(f"[回复] 处理异常: {e}")
            return None
        finally:
            tracker.analyzing = False
            self._proactive_flag[group_id] = False

    async def _handle_proactive(self, event: AstrMessageEvent, msg: dict, recent_window: List[dict]) -> Optional[MessageEventResult]:
        """处理主动模式。Bot 随机参与群聊，受 max_proactive_rounds 和 proactive_cooldown 限制。"""
        group_id = str(event.get_group_id())
        try:
            gname = ""
            try:
                g = await event.get_group()
                gname = g.group_name if g else ""
                if gname:
                    self.token_counter.set_group_name(group_id, gname)
            except Exception as e:
                self.logger.exception(f"[主动] 获取群名失败: {e}")

            context_lines = ["=== 群聊中的最近消息 ==="]
            all_image_urls = []
            for m in recent_window:
                context_lines.append(f"{m['user_name']}: {m['content']}")
                if m.get("image_urls"):
                    all_image_urls.extend(m["image_urls"])
            context_text = "\n".join(context_lines)

            self.logger.info(f"[主动] 分析群 {group_id} 是否应参与讨论...")
            analysis = await self._call_proactive_analyzer(context_text, image_urls=all_image_urls, umo=event.unified_msg_origin)
            if analysis is None:
                return None
            should_join = analysis.get("should_join", "no")
            reason = analysis.get("reason", "")
            if should_join == "no":
                self.logger.info(f"[主动] 群 {group_id} 不应参与 ({reason})")
                return None
            self.logger.info(f"[主动] 群 {group_id} 可以参与 | 原因: {reason}")

            reply_text = await self._call_generator_raw(context_text, image_urls=all_image_urls, umo=event.unified_msg_origin)
            if not reply_text:
                return None

            rounds = self._proactive_rounds.get(group_id, 0) + 1
            self._proactive_rounds[group_id] = rounds
            self.logger.info(f"[主动] 主动发言群 {group_id} (第{rounds}/{self._max_rounds()}轮): {reply_text[:60]}")
            self._proactive_flag[group_id] = True

            result = MessageEventResult()
            result.message(reply_text)
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            try:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    await conv_mgr.add_message_pair(
                        cid=cid,
                        user_message=UserMessageSegment(content=[TextPart(text=msg['content'])]),
                        assistant_message=AssistantMessageSegment(content=[TextPart(text=reply_text)]),
                    )
            except Exception as e:
                self.logger.exception(f"[主动] 写入会话历史失败: {e}")
            self._active_cooldowns[group_id] = time.time()
            if rounds >= self._max_rounds():
                self.logger.info(f"[主动] 群 {group_id} 已达到最大主动轮数，停止本轮主动参与")
            self._proactive_flag[group_id] = False
            return result

        except Exception as e:
            self.logger.exception(f"[主动] 处理异常: {e}")
            return None
        finally:
            self._active_thinking[group_id] = False
            self._proactive_flag[group_id] = False

    def _build_analyze_context(self, tracker: ConversationTracker):
        """构建分析上下文，返回 (context_text, image_urls)"""
        lines = []
        lines.append("=== Bot 刚才发出的消息 ===")
        lines.append(tracker.bot_message or "[Bot发送了一条消息]")
        lines.append(f"\n=== 触发者: {tracker.trigger_user_name} ===")
        lines.append(f"触发者消息: {tracker.trigger_message or '[未知]'}")
        lines.append(f"\n=== 群聊对话记录 ===")
        collected = tracker.collected
        all_image_urls = []
        if len(collected) > MAX_CONTEXT_MESSAGES:
            collected = collected[-MAX_CONTEXT_MESSAGES:]
            lines.append(f"[仅显示最近 {MAX_CONTEXT_MESSAGES} 条消息, 共 {len(tracker.collected)} 条]")
        for i, msg in enumerate(collected, 1):
            lines.append(f"{i}. {msg['user_name']}: {msg['content']}")
            if msg.get("image_urls"):
                all_image_urls.extend(msg["image_urls"])
        return "\n".join(lines), all_image_urls

    async def _call_proactive_analyzer(self, context_text: str, image_urls: list = None, umo: str = "") -> Optional[dict]:
        prompt = f"请分析以下群聊内容：\n\n{context_text}\n\n请判断Bot是否应该参与讨论。"
        provider_id = self._analyzer_provider()
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=self._proactive_analyzer_prompt(), image_urls=image_urls, umo=umo,
            )
            if not resp:
                return None
            return self._parse_json_response(resp)
        except Exception as e:
            self.logger.exception(f"[主动] LLM 分析失败: {e}")
            return None

    async def _call_analyzer(self, context_text: str, image_urls: list = None, umo: str = "") -> Optional[dict]:
        prompt = f"请分析以下群聊上下文：\n\n{context_text}\n\n请判断这些消息是否在回复Bot。"
        provider_id = self._analyzer_provider()
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=self._analyzer_prompt(), image_urls=image_urls, umo=umo,
            )
            if not resp:
                return None
            return self._parse_json_response(resp)
        except Exception as e:
            self.logger.exception(f"分析 LLM 调用失败: {e}")
            return None

    async def _build_generator_prompt(self, umo: str = "") -> str:
        """构造最终的 system_prompt：合并插件"简短回复"指令 + AstrBot 人格设定"""
        plugin_prompt = self._generator_prompt()
        try:
            personality = await self.context.persona_manager.get_default_persona_v3(umo)
            if personality and personality.get("prompt"):
                persona_text = personality["prompt"].strip()
                return f"{plugin_prompt}\n\n（来自人格设定）{persona_text}"
        except Exception as e:
            self.logger.exception(f"读取人格设定失败: {e}")
        return plugin_prompt

    async def _call_generator(self, context_text: str, image_urls: list = None, umo: str = "") -> Optional[str]:
        prompt = f"以下是群聊中的对话上下文：\n\n{context_text}\n\n请以Bot的身份自然地接上对话。"
        return await self._call_generator_raw(prompt, image_urls=image_urls, umo=umo)

    async def _call_generator_raw(self, prompt: str, image_urls: list = None, umo: str = "") -> Optional[str]:
        provider_id = self._generator_provider()
        system_prompt = await self._build_generator_prompt(umo)
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=system_prompt,
                image_urls=image_urls, umo=umo,
            )
            return resp.strip() if resp else None
        except Exception as e:
            self.logger.exception(f"生成 LLM 调用失败: {e}")
            return None

    async def _call_generator_with_tools(self, prompt: str, event: AstrMessageEvent, image_urls: list = None, umo: str = "") -> Optional[str]:
        provider_id = self._generator_provider()
        if not provider_id and umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception as e:
                self.logger.exception(f"获取 provider ID 失败: {e}")
                return None
        if not provider_id:
            return None
        try:
            tool_mgr = self.context.get_llm_tool_manager()
            tools = ToolSet(tool_mgr.func_list) if tool_mgr and tool_mgr.func_list else None
        except Exception as e:
            self.logger.exception(f"获取工具列表失败: {e}")
            tools = None
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        user_hint = f"\n[系统提示：当前群聊对话中，用户 {sender_name}（ID: {sender_id}）正在和 Bot 对话。所有需要指定用户的工具调用请使用此用户 ID。]"
        enhanced_prompt = prompt + user_hint
        system_prompt = await self._build_generator_prompt(umo)
        try:
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=enhanced_prompt,
                system_prompt=system_prompt,
                tools=tools,
                image_urls=image_urls,
                max_steps=10,
            )
            if resp is None:
                return None
            text = self._extract_bot_text(resp)
            if not text and (resp.tools_call_name or resp.result_chain):
                text = "[工具已执行操作]"
            gid = None
            if umo:
                try:
                    parts = umo.rsplit(":", 2)
                    if len(parts) >= 2:
                        gid = parts[-1]
                except Exception:
                    pass
            if gid:
                pt = ct = 0
                if hasattr(resp, 'usage') and resp.usage:
                    pt = getattr(resp.usage, 'input', 0) or 0
                    ct = getattr(resp.usage, 'output', 0) or 0
                if pt > 0 or ct > 0:
                    await self.token_counter.record(gid, pt, ct)
            return text.strip() if text else None
        except Exception as e:
            self.logger.exception(f"带工具的 LLM 调用失败: {e}")
            return None

    async def _call_llm(self, provider_id: Optional[str], prompt: str,
                        system_prompt: str = "", image_urls: list = None, umo: str = "") -> Optional[str]:
        if not provider_id and umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception as e:
                self.logger.exception(f"获取 provider ID 失败: {e}")
                return None
        if not provider_id:
            return None
        kwargs = dict(prompt=prompt, chat_provider_id=provider_id, image_urls=image_urls or None)
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        resp = await self.context.llm_generate(**kwargs)
        if resp is None:
            return None

        gid = None
        if umo:
            try:
                parts = umo.rsplit(":", 2)
                if len(parts) >= 2:
                    gid = parts[-1]
            except Exception:
                pass
        if gid:
            pt = ct = 0
            if hasattr(resp, 'usage') and resp.usage:
                pt = getattr(resp.usage, 'input', 0) or 0
                ct = getattr(resp.usage, 'output', 0) or 0
            if pt > 0 or ct > 0:
                await self.token_counter.record(gid, pt, ct)

        text = self._extract_bot_text(resp)
        return text.strip() if text else None

    @staticmethod
    def _parse_json_response(text: str) -> Optional[dict]:
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            json_str = m.group(1).strip()
        else:
            stack = []
            start = -1
            for i, ch in enumerate(text):
                if ch == '{':
                    if not stack:
                        start = i
                    stack.append(ch)
                elif ch == '}':
                    if stack:
                        stack.pop()
                        if not stack and start != -1:
                            json_str = text[start:i + 1]
                            break
            else:
                return None
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    async def page_token_stats(self):
        try:
            await self.token_counter.flush_all()
            from quart import request as qreq, jsonify
            period = qreq.args.get('period', 'all') if qreq else 'all'
            global_total = await self.token_counter.get_global_total(period)
            groups = await self.token_counter.get_all_groups_summary(period)
            return jsonify({
                "status": "ok",
                "data": {"global": global_total, "groups": groups}
            })
        except Exception as e:
            from quart import jsonify
            self.logger.exception(f"获取 token 统计失败: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def page_token_history(self):
        try:
            await self.token_counter.flush_all()
            from quart import request as qreq, jsonify
            days = int(qreq.args.get('days', 30)) if qreq else 30
            groups_data = await self.token_counter.get_all_groups_daily(min(days, 365))
            return jsonify({
                "status": "ok",
                "data": {"groups": groups_data}
            })
        except Exception as e:
            from quart import jsonify
            return jsonify({"status": "error", "message": str(e)})

    def _cleanup_tracker(self, group_id: str):
        tracker = self._trackers.pop(group_id, None)
        if tracker:
            tracker.alive = False

    async def terminate(self):
        self.logger.info("主动接话插件卸载中...")
        await self.token_counter.stop()
        for gid in list(self._trackers.keys()):
            self._cleanup_tracker(gid)
        self._trackers.clear()
        self._cooldowns.clear()
        self._active_cooldowns.clear()
        self._proactive_flag.clear()
        self._active_thinking.clear()
        self._recent_messages.clear()
        self._parsed_groups.clear()
        self._proactive_rounds.clear()
