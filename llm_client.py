import json
import re

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
        self,
        context_text: str,
        image_urls: list = None,
        umo: str = "",
        self_id: str = "",
        persona_name: str = "",
    ) -> dict | None:
        prompt = f"请分析以下群聊内容：\n\n{context_text}\n\n请判断你是否应该参与讨论。"
        provider_id = self.config_helper.analyzer_provider()
        system_prompt = self.config_helper.proactive_analyzer_prompt()
        system_prompt += (
            "\n\n请用以下 JSON 格式回复（请只输出 JSON 块，不要包含其他解释或标记）：\n"
            "注意：`should_join` 的值必须是布尔值（true 或 false，不需要双引号）。\n"
            "{\n"
            '  "should_join": true,\n'
            '  "reason": "原因说明"\n'
            "}"
        )

        identity_hints = []
        if self_id:
            identity_hints.append(f"你的账号ID/QQ号是: {self_id}")
        identity_str = "\n".join(identity_hints) if identity_hints else ""

        try:
            personality = await self.context.persona_manager.get_default_persona_v3(umo)
            persona_text = ""
            custom_prompt = self.config_helper.get_custom_persona_prompt(persona_name)
            if not custom_prompt and personality:
                p_name_db = personality.get("name")
                if p_name_db:
                    custom_prompt = self.config_helper.get_custom_persona_prompt(
                        p_name_db
                    )

            if custom_prompt:
                persona_text = custom_prompt.strip()
            elif personality and personality.get("prompt"):
                persona_text = personality["prompt"].strip()

            if persona_text:
                if identity_str:
                    persona_text = f"{identity_str}\n{persona_text}"
                system_prompt = f"<persona>\n{persona_text}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
            else:
                if identity_str:
                    system_prompt = f"<persona>\n{identity_str}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
                else:
                    system_prompt = (
                        f"<task_instructions>\n{system_prompt}\n</task_instructions>"
                    )
        except Exception as e:
            self.logger.exception(f"Failed to read persona setting: {e}")
            if identity_str:
                system_prompt = f"<persona>\n{identity_str}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
            else:
                system_prompt = (
                    f"<task_instructions>\n{system_prompt}\n</task_instructions>"
                )

        self.logger.debug(
            f"\n[AnalyzerPrompt] === Proactive Analysis LLM Call ===\n"
            f"--- System Prompt ---\n{system_prompt}\n"
            f"--- User Prompt ---\n{prompt}\n"
            f"============================================="
        )
        try:
            resp = await self.call_llm(
                provider_id=provider_id or None,
                prompt=prompt,
                system_prompt=system_prompt,
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
        self,
        context_text: str,
        image_urls: list = None,
        umo: str = "",
        self_id: str = "",
        persona_name: str = "",
    ) -> dict | None:
        prompt = f"请分析以下群聊上下文：\n\n{context_text}\n\n请判断现在是否需要发言。"
        provider_id = self.config_helper.analyzer_provider()
        system_prompt = self.config_helper.analyzer_prompt()
        system_prompt += (
            "\n\n请用以下 JSON 格式回复（请只输出 JSON 块，不要包含其他解释或标记）：\n"
            "注意：`need_reply` 的值必须是布尔值（true 或 false，不需要双引号）。\n"
            "{\n"
            '  "need_reply": true,\n'
            '  "reason": "原因说明"\n'
            "}"
        )

        identity_hints = []
        if self_id:
            identity_hints.append(f"你的账号ID/QQ号是: {self_id}")
        identity_str = "\n".join(identity_hints) if identity_hints else ""

        try:
            personality = await self.context.persona_manager.get_default_persona_v3(umo)
            persona_text = ""
            custom_prompt = self.config_helper.get_custom_persona_prompt(persona_name)
            if not custom_prompt and personality:
                p_name_db = personality.get("name")
                if p_name_db:
                    custom_prompt = self.config_helper.get_custom_persona_prompt(
                        p_name_db
                    )

            if custom_prompt:
                persona_text = custom_prompt.strip()
            elif personality and personality.get("prompt"):
                persona_text = personality["prompt"].strip()

            if persona_text:
                if identity_str:
                    persona_text = f"{identity_str}\n{persona_text}"
                system_prompt = f"<persona>\n{persona_text}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
            else:
                if identity_str:
                    system_prompt = f"<persona>\n{identity_str}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
                else:
                    system_prompt = (
                        f"<task_instructions>\n{system_prompt}\n</task_instructions>"
                    )
        except Exception as e:
            self.logger.exception(f"Failed to read persona setting: {e}")
            if identity_str:
                system_prompt = f"<persona>\n{identity_str}\n</persona>\n\n<task_instructions>\n{system_prompt}\n</task_instructions>"
            else:
                system_prompt = (
                    f"<task_instructions>\n{system_prompt}\n</task_instructions>"
                )

        self.logger.debug(
            f"\n[AnalyzerPrompt] === Reply Analysis LLM Call ===\n"
            f"--- System Prompt ---\n{system_prompt}\n"
            f"--- User Prompt ---\n{prompt}\n"
            f"=========================================="
        )
        try:
            resp = await self.call_llm(
                provider_id=provider_id or None,
                prompt=prompt,
                system_prompt=system_prompt,
                image_urls=image_urls,
                umo=umo,
            )
            if not resp:
                return None
            return parse_json_response(resp)
        except Exception as e:
            self.logger.exception(f"Analyzer LLM call failed: {e}")
            return None
