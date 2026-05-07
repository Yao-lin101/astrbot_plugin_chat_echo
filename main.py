"""
AstrBot 主动接话插件

功能：
Bot 发出消息后，自动监听后续群友的回复，使用 LLM 分析是否在跟 Bot 说话，
如果是，则使用 LLM 生成自然回复并主动发言。

双模式：
- 回复模式：Bot发言后跟踪窗口内群友消息，按概率分析是否回复Bot
- 主动模式：Bot没发言时，按概率随机抽取消息分析话题并主动参与讨论
"""

import asyncio
import json
import random
import re
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

from .utils.token_counter import TokenCounter

PLUGIN_NAME = "astrbot_plugin_chat_echo"

DEFAULT_ANALYZER_PROMPT = """你是一个群聊对话分析师。请分析以下群聊上下文，判断最新的几条消息是否在回复机器人(Bot)。

判断标准：
1. 消息中直接@了Bot或提到了Bot的名字
2. 消息内容明显是在回应Bot刚才说的内容
3. 对话上下文表明对方是在跟Bot说话
4. 如果消息是在跟其他人说话或闲聊，回复"no"
5. 如果有些消息是回复Bot的但有些不是，回复"partial"并简要说明

请用以下JSON格式回复（不要包含其他内容）：
{
  "is_reply_to_bot": "yes|no|partial",
  "reason": "简短的原因说明"
}"""

DEFAULT_GENERATOR_PROMPT = """你是一个活跃的群聊成员，正在参与群聊讨论。你的任务是自然地接上对话，表现得像一个真实的群友。

要求：
1. 回复要自然、口语化，不要太官方
2. 结合Bot刚才说的内容以及群友的回复来回应
3. 保持适度的个性，可以幽默风趣
4. 字数控制在50字以内
5. 不要使用表情符号或颜文字
6. 不要标记自己是AI或机器人
7. 回复要显得是对前面对话的自然延续"""

DEFAULT_PROACTIVE_ANALYZER_PROMPT = """你是一个群聊观察者。请分析以下群聊内容，判断Bot是否应该自然地参与讨论。

判断标准：
1. 群消息中提到的话题Bot是否了解或有见解
2. 对话是否在邀请更多人参与（提问、征求意见等）
3. Bot是否有一个自然的角度可以加入对话
4. 如果Bot参与会显得突兀，回复"no"
5. 如果Bot可以自然地接上话，回复"yes"

请用以下JSON格式回复（不要包含其他内容）：
{
  "should_join": "yes|no",
  "reason": "简短的原因说明"
}"""


class ConversationTracker:
    __slots__ = (
        "group_id", "unified_msg_origin", "bot_message",
        "trigger_user_name", "trigger_user_id",
        "collected", "expire_at",
        "analyzing", "round", "detection_count", "alive", "group_name",
    )

    def __init__(self, group_id: str, unified_msg_origin: str,
                 bot_message: str, trigger_user_name: str,
                 trigger_user_id: str, expire_seconds: int):
        self.group_id = group_id
        self.unified_msg_origin = unified_msg_origin
        self.bot_message = bot_message
        self.trigger_user_name = trigger_user_name
        self.trigger_user_id = trigger_user_id
        self.collected: List[dict] = []
        self.expire_at = time.time() + expire_seconds
        self.analyzing = False
        self.round = 0
        self.detection_count = 0
        self.alive = True
        # 群名（由 LLM 触发时填充）
        self.group_name = ""


@register("astrbot_plugin_chat_echo", "AMYdd00", "主动接话插件", "1.0.0")
class EchoPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.logger = logger
        self._trackers: Dict[str, ConversationTracker] = {}
        self._cooldowns: Dict[str, float] = {}
        self._proactive_flag = False
        self._active_thinking = False  # Route 2 互斥锁
        self._active_cooldowns: Dict[str, float] = {}  # Route 2 群冷却

        # ====== Token 统计 ======
        data_dir = StarTools.get_data_dir('chat_echo')
        self.token_counter = TokenCounter(data_dir)

        # 注册 Web API
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

    # ======================== 配置读取 ========================

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _is_enabled(self) -> bool:
        return bool(self._cfg("proactive_enabled", False))

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

    def _active_interval(self) -> int:
        return int(self._cfg("active_interval_minutes", 30)) * 60

    def _enabled_groups(self) -> list:
        return self._cfg("enabled_groups", [])

    def _max_rounds(self) -> int:
        return int(self._cfg("max_proactive_rounds", 3))

    def _cooldown(self) -> int:
        return int(self._cfg("proactive_cooldown_seconds", 300))

    def _analyzer_provider(self) -> str:
        return str(self._cfg("analyzer_provider_id", "") or "")

    def _generator_provider(self) -> str:
        return str(self._cfg("generator_provider_id", "") or "")

    def _analyzer_prompt(self) -> str:
        raw = self._cfg("analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_ANALYZER_PROMPT

    def _generator_prompt(self) -> str:
        raw = self._cfg("generator_system_prompt", "")
        return raw.strip() or DEFAULT_GENERATOR_PROMPT

    def _proactive_analyzer_prompt(self) -> str:
        raw = self._cfg("proactive_analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_PROACTIVE_ANALYZER_PROMPT

    # ======================== 群管理 ========================

    def _parse_group_entry(self, entry: str) -> Tuple[str, Optional[int], Optional[int]]:
        """解析群白名单条目，返回 (群标识, 回复概率, 主动概率)"""
        entry = entry.strip()
        if not entry:
            return "", None, None
        parts = entry.split(":")
        # 格式: "gid" / "gid:reply_prob" / "gid:reply_prob:active_prob"
        if len(parts) == 3 and parts[2].isdigit():
            return parts[0], int(parts[1]) if parts[1] else None, int(parts[2])
        elif len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1]), None
        else:
            return entry, None, None

    def _is_match_group(self, gid: str, event: AstrMessageEvent) -> bool:
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        if gid == group_id or gid == umo:
            return True
        try:
            if gid == umo.rsplit(":", 1)[-1]:
                return True
        except Exception:
            pass
        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        enabled = self._enabled_groups()
        if not enabled:
            return True
        for entry in enabled:
            gid, _, _ = self._parse_group_entry(entry)
            if self._is_match_group(gid, event):
                return True
        return False

    def _get_group_probability(self, event: AstrMessageEvent) -> Tuple[Optional[int], Optional[int]]:
        """返回 (回复概率, 主动概率)，None 表示用全局"""
        enabled = self._enabled_groups()
        if not enabled:
            return None, None
        for entry in enabled:
            gid, reply_p, active_p = self._parse_group_entry(entry)
            if self._is_match_group(gid, event):
                return reply_p, active_p
        return None, None

    # ======================== 核心判断 ========================

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:
            return False

    # ======================== 触发跟踪 ========================

    def _start_tracking(self, event: AstrMessageEvent, bot_message: str = ""):
        group_id = str(event.get_group_id())
        unified_msg_origin = event.unified_msg_origin
        sender_name = event.get_sender_name()
        sender_id = str(event.get_sender_id())
        now = time.time()
        last_time = self._cooldowns.get(group_id, 0)
        if now - last_time < self._cooldown():
            return
        if group_id in self._trackers and self._trackers[group_id].alive:
            return
        # 捕获群名
        try:
            gname = getattr(event, 'group_name', None) or ""
        except Exception:
            gname = ""
        tracker = ConversationTracker(
            group_id=group_id, unified_msg_origin=unified_msg_origin,
            bot_message=bot_message, trigger_user_name=sender_name,
            trigger_user_id=sender_id, expire_seconds=self._track_timeout(),
        )
        if gname:
            tracker.group_name = gname
        self._trackers[group_id] = tracker
        # 存入 TokenCounter
        if gname:
            self.token_counter.set_group_name(group_id, gname)

    # ======================== 事件：LLM 回复后 ========================

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        if not self._is_enabled() or self._proactive_flag:
            return
        if not self._is_group_event(event) or not self._is_group_allowed(event):
            return
        bot_text = ""
        if hasattr(response, "completion_text"):
            bot_text = response.completion_text or ""
        elif hasattr(response, "text"):
            bot_text = response.text or ""
        elif isinstance(response, str):
            bot_text = response
        if self._trigger_mode() in ("llm_response", "any_message"):
            self._start_tracking(event, bot_text)

    # ======================== 事件：消息发送后 ========================

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        if not self._is_enabled() or self._proactive_flag:
            return
        if not self._is_group_event(event) or not self._is_group_allowed(event):
            return
        if self._trigger_mode() != "any_message":
            return
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
        self._start_tracking(event, bot_text)

    # ======================== 双路线入口 ========================

    def _is_probability_hit(self, prob: int) -> bool:
        """概率是否命中"""
        return prob >= 100 or random.randint(1, 100) <= prob

    def _get_effective_reply_prob(self, event: AstrMessageEvent) -> int:
        gp, _ = self._get_group_probability(event)
        if gp is not None:
            return gp
        return self._global_probability()

    def _get_effective_active_prob(self, event: AstrMessageEvent) -> int:
        _, ap = self._get_group_probability(event)
        if ap is not None:
            return ap
        return self._active_probability()

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self._is_enabled() or not self._is_group_event(event):
            return
        if not self._is_group_allowed(event):
            return

        group_id = str(event.get_group_id())
        now = time.time()

        # 跳过 Bot 自己
        try:
            self_id = event.get_self_id()
            sender_id = event.get_sender_id()
            if self_id and sender_id and str(sender_id) == str(self_id):
                return
        except Exception:
            pass

        # 收集消息
        msg = {
            "user_name": event.get_sender_name(),
            "user_id": str(event.get_sender_id()),
            "content": event.message_str,
            "time": now,
        }

        # ====== Route 1: 回复模式（Bot已发言，跟踪窗口内）======
        tracker = self._trackers.get(group_id)
        if tracker and tracker.alive:
            if now > tracker.expire_at:
                self._cleanup_tracker(group_id)
            else:
                tracker.expire_at = now + self._track_timeout()
                tracker.collected.append(msg)
                # 互斥检查：Route 1 或 Route 2 正在处理中则跳过
                if tracker.analyzing or self._active_thinking:
                    return
                if tracker.round >= self._max_rounds():
                    self._cleanup_tracker(group_id)
                    return
                # 回复概率命中？
                if self._is_probability_hit(self._get_effective_reply_prob(event)):
                    tracker.analyzing = True
                    asyncio.create_task(self._handle_reply(tracker))
                return

        # ====== Route 2: 主动参与模式（Bot没发言时）======
        # 互斥检查
        if self._active_thinking or self._proactive_flag:
            return
        active_prob = self._get_effective_active_prob(event)
        if active_prob <= 0:
            return
        # 冷却检查
        last_active = self._active_cooldowns.get(group_id, 0)
        if now - last_active < self._active_interval():
            return
        # 主动概率命中？
        tracker_check = self._trackers.get(group_id)
        if tracker_check and tracker_check.alive:
            return  # 有活跃跟踪就走 Route 1
        if self._is_probability_hit(active_prob):
            self._active_thinking = True
            asyncio.create_task(self._handle_proactive(event, msg))

    # ======================== Route 1: 回复处理 ========================

    async def _handle_reply(self, tracker: ConversationTracker):
        group_id = tracker.group_id
        try:
            context_text = self._build_analyze_context(tracker)
            self.logger.info(f"[回复] 分析群 {group_id} 的回复是否针对 Bot...")
            analysis = await self._call_analyzer(context_text, umo=tracker.unified_msg_origin)
            if analysis is None:
                return
            is_reply = analysis.get("is_reply_to_bot", "no")
            reason = analysis.get("reason", "")
            if is_reply == "no":
                tracker.detection_count += 1
                max_detect = self._max_detection_count()
                self.logger.info(f"[回复] 群 {group_id} 不针对 Bot ({reason}) | {tracker.detection_count}/{max_detect}")
                if tracker.detection_count >= max_detect:
                    self.logger.info(f"[回复] 群 {group_id} 已达最大检测次数，停止")
                    self._cleanup_tracker(group_id)
                return
            self.logger.info(f"[回复] 群 {group_id} 的回复针对 Bot | 原因: {reason}")
            tracker.round += 1
            reply_text = await self._call_generator(context_text, umo=tracker.unified_msg_origin)
            if not reply_text:
                return
            self.logger.info(f"[回复] 主动回复群 {group_id} (第{tracker.round}轮): {reply_text[:60]}")
            self._proactive_flag = True
            try:
                chain = MessageChain()
                chain.message(reply_text)
                await self.context.send_message(tracker.unified_msg_origin, chain)
                self._cooldowns[group_id] = time.time()
                tracker.detection_count = 0
                tracker.expire_at = time.time() + self._track_timeout()
                if tracker.round >= self._max_rounds():
                    self.logger.info(f"[回复] 群 {group_id} 已达到最大主动回复轮数")
                    self._cleanup_tracker(group_id)
            except Exception as e:
                self.logger.error(f"[回复] 发送消息失败: {e}")
            finally:
                self._proactive_flag = False
        finally:
            tracker.analyzing = False

    # ======================== Route 2: 主动参与处理 ========================

    async def _handle_proactive(self, event: AstrMessageEvent, msg: dict):
        group_id = str(event.get_group_id())
        try:
            # 捕获群名
            try:
                gname = getattr(event, 'group_name', None) or ""
                if gname:
                    self.token_counter.set_group_name(group_id, gname)
            except Exception:
                pass
            # 构建简短上下文（只有这一条消息+附近几条收集的消息）
            context_text = (
                f"=== 群聊中的最新消息 ===\n"
                f"{msg['user_name']}: {msg['content']}"
            )
            self.logger.info(f"[主动] 分析群 {group_id} 是否应参与讨论...")
            analysis = await self._call_proactive_analyzer(context_text, umo=event.unified_msg_origin)
            if analysis is None:
                return
            should_join = analysis.get("should_join", "no")
            reason = analysis.get("reason", "")
            if should_join == "no":
                self.logger.info(f"[主动] 群 {group_id} 不应参与 ({reason})")
                return
            self.logger.info(f"[主动] 群 {group_id} 可以参与 | 原因: {reason}")
            # 生成参与发言
            prompt = (
                f"以下是群聊中的最新消息：\n\n{context_text}\n\n"
                f"请以Bot的身份自然地加入讨论。"
            )
            reply_text = await self._call_generator_raw(prompt, umo=event.unified_msg_origin)
            if not reply_text:
                return
            self.logger.info(f"[主动] 主动发言群 {group_id}: {reply_text[:60]}")
            self._proactive_flag = True
            try:
                chain = MessageChain()
                chain.message(reply_text)
                await self.context.send_message(event.unified_msg_origin, chain)
                self._active_cooldowns[group_id] = time.time()
            except Exception as e:
                self.logger.error(f"[主动] 发送消息失败: {e}")
            finally:
                self._proactive_flag = False
        finally:
            self._active_thinking = False

    # ======================== 构建上下文 ========================

    def _build_analyze_context(self, tracker: ConversationTracker) -> str:
        lines = []
        lines.append("=== Bot 刚才发出的消息 ===")
        lines.append(tracker.bot_message or "[Bot发送了一条消息]")
        lines.append(f"\n=== 触发者: {tracker.trigger_user_name} ===")
        lines.append(f"\n=== 群聊对话记录 ===")
        for i, msg in enumerate(tracker.collected, 1):
            lines.append(f"{i}. {msg['user_name']}: {msg['content']}")
        return "\n".join(lines)

    # ======================== LLM 调用 ========================

    async def _call_proactive_analyzer(self, context_text: str, umo: str = "") -> Optional[dict]:
        prompt = f"请分析以下群聊内容：\n\n{context_text}\n\n请判断Bot是否应该参与讨论。"
        provider_id = self._analyzer_provider()
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=self._proactive_analyzer_prompt(), umo=umo,
            )
            if not resp:
                return None
            return self._parse_json_response(resp)
        except Exception as e:
            self.logger.error(f"[主动] LLM 分析失败: {e}")
            return None

    async def _call_analyzer(self, context_text: str, umo: str = "") -> Optional[dict]:
        prompt = f"请分析以下群聊上下文：\n\n{context_text}\n\n请判断这些消息是否在回复Bot。"
        provider_id = self._analyzer_provider()
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=self._analyzer_prompt(), umo=umo,
            )
            if not resp:
                return None
            return self._parse_json_response(resp)
        except Exception as e:
            self.logger.error(f"分析 LLM 调用失败: {e}")
            return None

    async def _call_generator(self, context_text: str, umo: str = "") -> Optional[str]:
        prompt = f"以下是群聊中的对话上下文：\n\n{context_text}\n\n请以Bot的身份自然地接上对话。"
        return await self._call_generator_raw(prompt, umo)

    async def _call_generator_raw(self, prompt: str, umo: str = "") -> Optional[str]:
        provider_id = self._generator_provider()
        try:
            resp = await self._call_llm(
                provider_id=provider_id or None, prompt=prompt,
                system_prompt=self._generator_prompt(), umo=umo,
            )
            return resp.strip() if resp else None
        except Exception as e:
            self.logger.error(f"生成 LLM 调用失败: {e}")
            return None

    async def _call_llm(self, provider_id: Optional[str], prompt: str,
                        system_prompt: str, umo: str = "") -> Optional[str]:
        if not provider_id and umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(None)
                except Exception:
                    return None
        if not provider_id:
            return None
        resp = await self.context.llm_generate(
            prompt=prompt, system_prompt=system_prompt, chat_provider_id=provider_id,
        )
        if resp is None:
            return None

        # ====== Token 记录 ======
        group_id = None
        if umo:
            try:
                group_id = umo.rsplit(":", 1)[-1]
            except Exception:
                pass
        if group_id:
            prompt_tokens = 0
            completion_tokens = 0
            if hasattr(resp, 'usage') and resp.usage:
                prompt_tokens = getattr(resp.usage, 'input', 0) or 0
                completion_tokens = getattr(resp.usage, 'output', 0) or 0
            if prompt_tokens > 0 or completion_tokens > 0:
                await self.token_counter.record(group_id, prompt_tokens, completion_tokens)

        if hasattr(resp, "completion_text"):
            text = resp.completion_text
        elif hasattr(resp, "text"):
            text = resp.text
        elif isinstance(resp, str):
            text = resp
        else:
            text = str(resp)
        return text.strip() if text else None

    @staticmethod
    def _parse_json_response(text: str) -> Optional[dict]:
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            json_str = m.group(1).strip()
        else:
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e != -1:
                json_str = text[s:e + 1]
            else:
                return None
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    # ======================== Token 统计 Web API ========================

    async def page_token_stats(self):
        """获取 Token 统计数据"""
        try:
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
            self.logger.error(f"获取 token 统计失败: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def page_token_history(self):
        """获取历史趋势数据（多群多线）"""
        try:
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

    # ======================== 清理 ========================

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
