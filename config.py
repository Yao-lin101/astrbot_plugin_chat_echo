from pathlib import Path

DEFAULT_ANALYZER_PROMPT = """你是一个群聊对话分析师。请分析以下群聊上下文，判断群友的最新消息是否在和你对话。

判断标准：
1. 你上一条/上几条消息是你发的，群友紧接着你发言 → 默认是在对话
2. 消息中提到你的名字或@了你 → 肯定是在对话
3. 群友在跟别人说话（叫了别人名字、回应非你的消息，或者@了别人、回复了别人）→ 不是在对话
4. 群友只是发纯语气词/表情且无任何上下文关联 → 不是在对话
5. 群消息以 / ! # $ & 等符号开头，说明是发给机器人的指令 → 不是在对话

注意：如果消息中明确@了其他人，或者回复引用了其他人的消息（而非你），这属于在跟别人说话，判断结果应为不是在对话（"no"）。

核心逻辑：你刚说完话就有人接话，正常聊天中就是对方在回应你。但如果对方明确是在和别人互动，则排除。
宁可误判也不要漏判。

请用以下JSON格式回复（不要包含其他内容）：
{
  "is_reply_to_bot": "yes|no",
  "reason": "简短的原因说明"
}"""

DEFAULT_PROACTIVE_ANALYZER_PROMPT = """你是一个群聊观察者。请分析以下群聊内容，判断你是否应该参与讨论。

判断标准：
1. 群消息中提到的话题你是否了解或有见解
2. 对话是否在邀请更多人参与（提问、征求意见等）
3. 你是否有想说的内容（吐槽、补充、提问、分享等）
4. 只要是你有内容想回复，就回复"yes"
5. 只有你完全无话可说时才回复"no"
6. 群消息以 / ! # $ & 等符号开头，说明是发给机器人的指令 → 不应参与

请用以下JSON格式回复（不要包含其他内容）：
{
  "should_join": "yes|no",
  "reason": "简短的原因说明"
}"""

CONFIG_VERSION = 5
CONFIG_VERSION_FILE = "_config_version.txt"


def _is_valid_entry(entry) -> bool:
    """Check if a config list entry is valid (non-empty dict or non-empty string)."""
    if isinstance(entry, dict):
        return True
    return bool(str(entry).strip())


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
        else:
            config.proactive_analyzer_system_prompt = DEFAULT_PROACTIVE_ANALYZER_PROMPT
            config.analyzer_system_prompt = DEFAULT_ANALYZER_PROMPT
        if hasattr(config, "save_config"):
            config.save_config()
        ver_file.parent.mkdir(parents=True, exist_ok=True)
        ver_file.write_text(str(CONFIG_VERSION))
        logger.info(f"Config upgraded and saved (v{CONFIG_VERSION})")
    except Exception as e:
        logger.exception(f"Failed to upgrade config: {e}")


def parse_group_entry(entry) -> list[tuple[str, int | None, int | None]]:
    """Parse a group whitelist entry. Supports both old string format and new dict format.
    Returns a list of (group_id, reply_prob, active_prob) tuples (supports comma-separated multi-group).
    """
    rp: int | None = None
    ap: int | None = None
    gid_str = ""

    # New template_list dict format
    if isinstance(entry, dict):
        gid_str = entry.get("group_id", "").strip()
        rp = entry.get("reply_probability")
        ap = entry.get("active_probability")
        if not (isinstance(rp, int) and rp >= 0):
            rp = None
        if not (isinstance(ap, int) and ap >= 0):
            ap = None
    else:
        # Old string format: "group_id:reply_prob:active_prob"
        entry = str(entry).strip()
        if not entry:
            return []
        parts = entry.split(":")

        platforms = {
            "aiocqhttp", "telegram", "discord", "lark", "qq_official",
            "dingtalk", "kook", "slack", "mattermost", "satori",
        }
        is_umo = False
        if len(parts) >= 3:
            if parts[0] in platforms or parts[1] in {
                "GroupMessage", "PrivateMessage", "GuildMessage",
            }:
                is_umo = True

        if is_umo:
            gid_str = ":".join(parts[:3])
            if len(parts) >= 4:
                rp = int(parts[3]) if parts[3].strip().isdigit() else None
            if len(parts) >= 5:
                ap = int(parts[4]) if parts[4].strip().isdigit() else None
        else:
            gid_str = parts[0]
            if len(parts) >= 2:
                rp = int(parts[1]) if parts[1].strip().isdigit() else None
            if len(parts) >= 3:
                ap = int(parts[2]) if parts[2].strip().isdigit() else None

    # Split comma-separated group IDs
    if not gid_str:
        return []
    return [(g.strip(), rp, ap) for g in _split_list(gid_str)]


def _split_list(value: str | None) -> list[str]:
    """Split comma-separated string (supports both Chinese and English commas)."""
    if not value:
        return []
    normalized = str(value).replace("，", ",")
    return [s.strip() for s in normalized.split(",") if s.strip()]


def parse_keyword_rule(entry) -> tuple[list[str], set[str], int | None]:
    """Parse a keyword rule entry. Returns (keywords, groups_set, probability).
    Supports both old string format and new dict format.
    """
    # New template_list dict format (v1.1.3+)
    if isinstance(entry, dict):
        kw_str = entry.get("keywords", entry.get("keyword", ""))
        groups_str = entry.get("groups", "")
        keywords = _split_list(kw_str)
        groups = set(_split_list(groups_str))
        prob = entry.get("probability")
        if isinstance(prob, int) and prob >= 0:
            return keywords, groups, prob
        return keywords, groups, None

    # Old string format: "keyword" or "keyword:probability"
    entry = str(entry).strip()
    if not entry:
        return [], set(), None
    if ":" in entry:
        parts = entry.rsplit(":", 1)
        keyword = parts[0].strip()
        prob_str = parts[1].strip()
        prob = int(prob_str) if prob_str.isdigit() else None
        return [keyword] if keyword else [], set(), prob
    return [entry], set(), None


class ConfigHelper:
    """Helper to manage and access configuration parameters."""

    def __init__(self, config):
        self.config = config
        self.parsed_groups: list[tuple[str, int | None, int | None]] = []
        self.parsed_keywords: list[tuple[list[str], set[str], int | None]] = []
        self.refresh()

    def refresh(self):
        """Re-parse the enabled groups from configuration."""
        enabled = self.enabled_groups()
        self.parsed_groups = []
        for entry in enabled:
            if _is_valid_entry(entry):
                self.parsed_groups.extend(parse_group_entry(entry))

        keywords = self.keyword_rules()
        self.parsed_keywords = [
            parse_keyword_rule(entry) for entry in keywords if _is_valid_entry(entry)
        ]

    def cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def trigger_mode(self) -> str:
        return str(self.cfg("trigger_mode", "llm_response"))

    def enable_keyword_trigger(self) -> bool:
        return bool(self.cfg("enable_keyword_trigger", False))

    def enable_keyword_on_image(self) -> bool:
        return bool(self.cfg("enable_keyword_on_image", False))

    def keyword_rules(self) -> list:
        return self.cfg("keyword_rules", [])

    def keyword_default_probability(self) -> int:
        return int(self.cfg("keyword_default_probability", 100))

    def get_matched_keyword(self, group_id: str, content: str) -> tuple[str | None, int | None]:
        """Check if content matches any keyword rule applicable to this group.
        Returns (matched_keyword, probability) or (None, None).
        """
        content_lower = content.lower()
        for keywords, groups, prob in self.parsed_keywords:
            # Check group filter: support group_id, UMO, and UMO suffix matching
            if groups and not self._match_group_set(groups, group_id):
                continue
            for kw in keywords:
                if kw.lower() in content_lower:
                    return kw, prob
        return None, None

    def _match_group_set(self, groups: set[str], group_id: str) -> bool:
        """Check if group_id matches any entry in the groups set.
        Supports direct group_id match and UMO suffix match (e.g., Bot:GroupMessage:123).
        """
        for g in groups:
            if g == group_id:
                return True
            # UMO suffix match: the last segment after the last ':' equals group_id
            try:
                if ":" in g and g.rsplit(":", 1)[-1] == group_id:
                    return True
            except (AttributeError, IndexError, ValueError):
                pass
        return False

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

    def enable_image_caption(self) -> bool:
        return bool(self.cfg("enable_image_caption", False))

    def image_caption_probability(self) -> int:
        return int(self.cfg("image_caption_probability", 100))

    def image_caption_provider(self) -> str:
        return str(self.cfg("image_caption_provider_id", "") or "")

    def enable_llm_tools(self) -> bool:
        return bool(self.cfg("enable_llm_tools", True))

    def analyzer_prompt(self) -> str:
        raw = self.cfg("analyzer_system_prompt", "")
        return raw.strip() or DEFAULT_ANALYZER_PROMPT

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
        return True  # 白名单仅控制概率覆盖，不做硬拦截

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

    def human_like_mode(self) -> bool:
        return bool(self.cfg("human_like_mode", False))

    def wake_at_threshold(self) -> int:
        return int(self.cfg("wake_at_threshold", 3))

    def wake_window_minutes(self) -> int:
        return int(self.cfg("wake_window_minutes", 30))

    def typing_delay_min(self) -> float:
        return float(self.cfg("typing_delay_min", 1.5))

    def typing_delay_max(self) -> float:
        return float(self.cfg("typing_delay_max", 4.0))

    # ======== Batch analysis config ========

    def batch_analysis_enabled(self) -> bool:
        return bool(self.cfg("batch_analysis_enabled", True))

    def silence_multiplier(self) -> float:
        return float(self.cfg("silence_multiplier", 2.5))

    def min_silence_seconds(self) -> int:
        return int(self.cfg("min_silence_seconds", 3))

    def max_silence_seconds(self) -> int:
        return int(self.cfg("max_silence_seconds", 12))

    def max_batch_wait_seconds(self) -> int:
        return int(self.cfg("max_batch_wait_seconds", 15))

    def max_batch_messages(self) -> int:
        return int(self.cfg("max_batch_messages", 6))

    def instant_at_bot(self) -> bool:
        return bool(self.cfg("instant_at_bot", True))

    def caption_timeout_seconds(self) -> int:
        return int(self.cfg("caption_timeout_seconds", 10))

    def caption_timeout_behavior(self) -> str:
        return str(self.cfg("caption_timeout_behavior", "wait_then_fallback"))