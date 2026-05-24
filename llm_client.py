import json
import re

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import ToolSet

from .helpers import extract_bot_text


def parse_json_response(text: str) -> dict | None:
    """Parse JSON from LLM response text, handling formatting quirks and markdown."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        json_str = m.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            text_to_search = json_str
    else:
        text_to_search = text

    stack = []
    start = -1
    for i, ch in enumerate(text_to_search):
        if ch == "{":
            if not stack:
                start = i
            stack.append(ch)
        elif ch == "}":
            if stack:
                stack.pop()
                if not stack and start != -1:
                    json_str = text_to_search[start : i + 1]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        pass
                    break
    return None


class LLMHandler:
    """Handles communication with LLM provider, prompt customization, and execution."""

    def __init__(self, context, config_helper, token_counter, logger):
        self.context = context
        self.config_helper = config_helper
        self.token_counter = token_counter
        self.logger = logger

    async def call_llm(
        self,
        provider_id: str | None,
        prompt: str,
        system_prompt: str = "",
        image_urls: list = None,
        umo: str = "",
    ) -> str | None:
        if not provider_id and umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception as e:
                self.logger.exception(f"Failed to get provider ID: {e}")
                return None
        if not provider_id:
            return None

        kwargs = {
            "prompt": prompt,
            "chat_provider_id": provider_id,
            "image_urls": image_urls or None,
        }
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
            if hasattr(resp, "usage") and resp.usage:
                pt = getattr(resp.usage, "input", 0) or 0
                ct = getattr(resp.usage, "output", 0) or 0
            if pt > 0 or ct > 0:
                await self.token_counter.record(gid, pt, ct)

        text = extract_bot_text(resp)
        return text.strip() if text else None

    async def call_proactive_analyzer(
        self, context_text: str, image_urls: list = None, umo: str = ""
    ) -> dict | None:
        prompt = (
            f"请分析以下群聊内容：\n\n{context_text}\n\n请判断Bot是否应该参与讨论。"
        )
        provider_id = self.config_helper.analyzer_provider()
        try:
            resp = await self.call_llm(
                provider_id=provider_id or None,
                prompt=prompt,
                system_prompt=self.config_helper.proactive_analyzer_prompt(),
                image_urls=image_urls,
                umo=umo,
            )
            if not resp:
                return None
            return parse_json_response(resp)
        except Exception as e:
            self.logger.exception(f"[Proactive] LLM analysis failed: {e}")
            return None

    async def call_analyzer(
        self, context_text: str, image_urls: list = None, umo: str = ""
    ) -> dict | None:
        prompt = (
            f"请分析以下群聊上下文：\n\n{context_text}\n\n请判断这些消息是否在回复Bot。"
        )
        provider_id = self.config_helper.analyzer_provider()
        try:
            resp = await self.call_llm(
                provider_id=provider_id or None,
                prompt=prompt,
                system_prompt=self.config_helper.analyzer_prompt(),
                image_urls=image_urls,
                umo=umo,
            )
            if not resp:
                return None
            return parse_json_response(resp)
        except Exception as e:
            self.logger.exception(f"Analyzer LLM call failed: {e}")
            return None

    async def build_generator_prompt(self, umo: str = "") -> str:
        """Build the final system prompt: merging the plugin prompt guidelines with the personality setup."""
        plugin_prompt = self.config_helper.generator_prompt()
        try:
            personality = await self.context.persona_manager.get_default_persona_v3(umo)
            if personality and personality.get("prompt"):
                persona_text = personality["prompt"].strip()
                return f"{plugin_prompt}\n\n（来自人格设定）{persona_text}"
        except Exception as e:
            self.logger.exception(f"Failed to read persona setting: {e}")
        return plugin_prompt

    async def call_generator(
        self, context_text: str, image_urls: list = None, umo: str = ""
    ) -> str | None:
        prompt = f"以下是群聊中的对话上下文：\n\n{context_text}\n\n请以Bot的身份自然地接上对话。"
        return await self.call_generator_raw(prompt, image_urls=image_urls, umo=umo)

    async def call_generator_raw(
        self, prompt: str, image_urls: list = None, umo: str = ""
    ) -> str | None:
        provider_id = self.config_helper.generator_provider()
        system_prompt = await self.build_generator_prompt(umo)
        try:
            resp = await self.call_llm(
                provider_id=provider_id or None,
                prompt=prompt,
                system_prompt=system_prompt,
                image_urls=image_urls,
                umo=umo,
            )
            return resp.strip() if resp else None
        except Exception as e:
            self.logger.exception(f"Generator LLM call failed: {e}")
            return None

    async def call_generator_with_tools(
        self,
        prompt: str,
        event: AstrMessageEvent,
        image_urls: list = None,
        umo: str = "",
    ) -> str | None:
        provider_id = self.config_helper.generator_provider()
        if not provider_id and umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception as e:
                self.logger.exception(f"Failed to get provider ID: {e}")
                return None
        if not provider_id:
            return None
        try:
            tool_mgr = self.context.get_llm_tool_manager()
            tools = (
                ToolSet(tool_mgr.func_list) if tool_mgr and tool_mgr.func_list else None
            )
        except Exception as e:
            self.logger.exception(f"Failed to get tool list: {e}")
            tools = None
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        user_hint = f"\n[系统提示：当前群聊对话中，用户 {sender_name}（ID: {sender_id}）正在和 Bot 对话。所有需要指定用户的工具调用请使用此用户 ID。]"
        enhanced_prompt = prompt + user_hint
        system_prompt = await self.build_generator_prompt(umo)
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
            text = extract_bot_text(resp)
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
                if hasattr(resp, "usage") and resp.usage:
                    pt = getattr(resp.usage, "input", 0) or 0
                    ct = getattr(resp.usage, "output", 0) or 0
                if pt > 0 or ct > 0:
                    await self.token_counter.record(gid, pt, ct)
            return text.strip() if text else None
        except Exception as e:
            self.logger.exception(f"LLM call with tools failed: {e}")
            return None
