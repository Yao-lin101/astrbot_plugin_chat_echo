from pathlib import Path

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
- 结合Bot刚才说的内容 and 群友的回复来回应
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
CONFIG_VERSION_FILE = "_config_version.txt"


def upgrade_config(config, data_dir: Path, logger) -> None:
    """Upgrade configuration prompts if they are outdated."""
    try:
        ver_file = data_dir / CONFIG_VERSION_FILE
        cfg_version = 0
        if ver_file.exists():
            cfg_version = int(ver_file.read_text().strip())
        if cfg_version >= CONFIG_VERSION:
            return
        logger.info(
            f"Detected config version {cfg_version}, upgrading to {CONFIG_VERSION}, prompts will be overwritten..."
        )
        if isinstance(config, dict):
            config["proactive_analyzer_system_prompt"] = (
                DEFAULT_PROACTIVE_ANALYZER_PROMPT
            )
            config["analyzer_system_prompt"] = DEFAULT_ANALYZER_PROMPT
            config["generator_system_prompt"] = DEFAULT_GENERATOR_PROMPT
        else:
            config.proactive_analyzer_system_prompt = DEFAULT_PROACTIVE_ANALYZER_PROMPT
            config.analyzer_system_prompt = DEFAULT_ANALYZER_PROMPT
            config.generator_system_prompt = DEFAULT_GENERATOR_PROMPT
        if hasattr(config, "save_config"):
            config.save_config()
        ver_file.parent.mkdir(parents=True, exist_ok=True)
        ver_file.write_text(str(CONFIG_VERSION))
        logger.info(f"Config upgraded and saved (v{CONFIG_VERSION})")
    except Exception as e:
        logger.exception(f"Failed to upgrade config: {e}")


def parse_group_entry(entry: str) -> tuple[str, int | None, int | None]:
    """Parse a group whitelist entry."""
    entry = entry.strip()
    if not entry:
        return "", None, None
    parts = entry.split(":")

    platforms = {
        "aiocqhttp",
        "telegram",
        "discord",
        "lark",
        "qq_official",
        "dingtalk",
        "kook",
        "slack",
        "mattermost",
        "satori",
    }

    is_umo = False
    if len(parts) >= 3:
        if parts[0] in platforms or parts[1] in {
            "GroupMessage",
            "PrivateMessage",
            "GuildMessage",
        }:
            is_umo = True

    if is_umo:
        umo_id = ":".join(parts[:3])
        reply_p = None
        active_p = None
        if len(parts) >= 4:
            reply_p = int(parts[3]) if parts[3].strip().isdigit() else None
        if len(parts) >= 5:
            active_p = int(parts[4]) if parts[4].strip().isdigit() else None
        return umo_id, reply_p, active_p
    else:
        group_id = parts[0]
        reply_p = None
        active_p = None
        if len(parts) >= 2:
            reply_p = int(parts[1]) if parts[1].strip().isdigit() else None
        if len(parts) >= 3:
            active_p = int(parts[2]) if parts[2].strip().isdigit() else None
        return group_id, reply_p, active_p


class ConfigHelper:
    """Helper to manage and access configuration parameters."""

    def __init__(self, config):
        self.config = config
        self.parsed_groups: list[tuple[str, int | None, int | None]] = []
        self.refresh()

    def refresh(self):
        """Re-parse the enabled groups from configuration."""
        enabled = self.enabled_groups()
        self.parsed_groups = [
            parse_group_entry(entry) for entry in enabled if entry.strip()
        ]

    def cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def trigger_mode(self) -> str:
        return str(self.cfg("trigger_mode", "llm_response"))

    def track_timeout(self) -> int:
        return int(self.cfg("track_timeout_seconds", 120))

    def max_detection_count(self) -> int:
        return int(self.cfg("max_detection_count", 10))

    def reply_probability(self) -> int:
        return int(self.cfg("reply_probability", 100))

    def active_probability(self) -> int:
        return int(self.cfg("active_probability", 0))

    def enabled_groups(self) -> list:
        return self.cfg("enabled_groups", [])

    def max_rounds(self) -> int:
        return int(self.cfg("max_proactive_rounds", 3))

    def proactive_cooldown(self) -> int:
        return int(self.cfg("proactive_cooldown_seconds", 300))

    def analyzer_provider(self) -> str:
        return str(self.cfg("analyzer_provider_id", "") or "")

    def generator_provider(self) -> str:
        return str(self.cfg("generator_provider_id", "") or "")

    def enable_llm_tools(self) -> bool:
        return bool(self.cfg("enable_llm_tools", True))

    def analyzer_prompt(self) -> str:
        raw = self.cfg("analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_ANALYZER_PROMPT

    def generator_prompt(self) -> str:
        raw = self.cfg("generator_system_prompt", "")
        return raw.strip() or DEFAULT_GENERATOR_PROMPT

    def proactive_analyzer_prompt(self) -> str:
        raw = self.cfg("proactive_analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_PROACTIVE_ANALYZER_PROMPT

    def is_match_group(self, gid: str, group_id: str, umo: str) -> bool:
        if gid == group_id or gid == umo:
            return True
        try:
            if gid == umo.rsplit(":", 1)[-1]:
                return True
        except (AttributeError, IndexError, ValueError):
            pass
        return False

    def is_group_allowed(self, group_id: str, umo: str) -> bool:
        if not self.parsed_groups:
            return True
        for gid, _, _ in self.parsed_groups:
            if self.is_match_group(gid, group_id, umo):
                return True
        return False

    def get_group_probability(
        self, group_id: str, umo: str
    ) -> tuple[int | None, int | None]:
        if not self.parsed_groups:
            return None, None
        for gid, reply_p, active_p in self.parsed_groups:
            if self.is_match_group(gid, group_id, umo):
                return reply_p, active_p
        return None, None

    def get_effective_reply_prob(self, group_id: str, umo: str) -> int:
        gp, _ = self.get_group_probability(group_id, umo)
        if gp is not None:
            return gp
        return self.reply_probability()

    def get_effective_active_prob(self, group_id: str, umo: str) -> int:
        _, ap = self.get_group_probability(group_id, umo)
        if ap is not None:
            return ap
        return self.active_probability()
