# Changelog

## [1.0.3] - 2025-05-21

### 修复
- 🐛 修复 `_proactive_flag` 在 `finally` 中提前重置导致并发防护失效的核心 Bug
- 🐛 修复 `max_proactive_rounds` 错误地限制回复模式（@bot 对话），现仅影响主动模式（Route 2）
- 🐛 修复 `proactive_cooldown_seconds` 从未被使用的死代码问题，现真正应用于主动模式冷却
- 🐛 修复函数返回类型标注 `MessageChain` 应为 `MessageEventResult`
- 🐛 修复 `_upgrade_config` 将 `_config_version` 写入配置文件污染 `_conf_schema.json`，现使用独立版本文件

### 优化
- 🔧 回复模式（Route 1）与主动模式（Route 2）逻辑完全解耦，@bot 对话不受轮数和冷却限制
- 🔧 新增 `_proactive_rounds` 字典单独追踪主动模式轮数
- 🔧 移除冗余配置 `reply_cooldown_seconds`（由 `track_timeout_seconds` 控制即可）
- 🔧 移除废弃配置 `active_interval_minutes`
- 🔧 函数内 `import` 移至文件顶部，减少运行时开销
- 🔧 `terminate()` 中补全清理所有字典（`_proactive_flag`、`_active_thinking`、`_recent_messages` 等）
- 🔧 修正 `enable_llm_tools` 配置描述中的误导性表述

## [1.0.2] - 2025-05-21

### 修复
- 🐛 修复回复模式生成"小作文"问题：将"简短回复"指令作为真正的 `system_prompt` 传入，而非拼接到 user prompt 末尾
- 🐛 修复人格设定（如"不要使用表情"）被忽略的问题：从 `persona_manager` 读取人格设定合并到 system prompt
- 🐛 修复 `_handle_reply` 中 round 在 LLM 调用前递增，导致 LLM 失败时浪费轮数
- 🐛 修复 `_call_generator_with_tools` 传入整个对话历史（970条/847k tokens），改为只传合并后的 system prompt
- 🐛 清理 `_call_llm` 中残留的 `_load_contexts` 和 contexts 加载逻辑

### 优化
- 🔧 新增 `_build_generator_prompt` 方法，从 `persona_manager.get_default_persona_v3` 读取人格设定合并到 system_prompt
- 🔧 不在生成回复时传入对话历史，大幅减少 token 消耗

## [1.0.1] - 2025-05-09

### 修复
- 🐛 修复 `_build_analyze_context` 缺少触发者消息内容，LLM 分析准确率提升
- 🐛 修复 `TokenCounter._cache` 初始化不一致可能导致的崩溃
- 🐛 修复全局单例锁导致群间上下文串扰，改为 per-group 独立锁
- 🐛 修复 `from astrbot.core.agent.tool import ToolSet` 导入导致插件加载崩溃
- 🐛 修复工具调用（发图/戳一戳等）后无文本导致流程中断
- 🐛 修复图片 URL 传给不支持多模态的模型报错
- 🐛 修复 Bot 读取 QQ 号错误，在 prompt 中注入真实 sender_id
- 🐛 修复 `_get_period_range("year")" 语义偏差 → WebUI"本年"改为"近一年"
- 🐛 修复 `_config_version` 写进配置污染 WebUI 配置页面

### 新增
- ✨ 回复模式支持调用 AstrBot 所有已注册工具/技能（搜索、发图、Function Calling），通过 `enable_llm_tools` 开关控制
- ✨ Route 2（主动模式）新增滚动消息窗口，保留最近 10 条消息作为上下文
- ✨ 图片/表情包识别：提取图片 URL 传给多模态 LLM 分析判断是否应回复
- ✨ 群白名单预解析缓存，提升性能
- ✨ 配置版本自动升级机制，版本号独立文件存储

### 优化
- 🔧 JSON 解析器改用括号匹配算法，提高解析准确率
- 🔧 上下文截断（最多保留 20 条），防止 Token 膨胀
- 🔧 异常处理使用 `logger.exception` 保留完整堆栈
- 🔧 收窄异常类型，避免吞掉关键信号
- 🔧 提取公共方法 `_extract_bot_text` 和 `_extract_sent_text`
- 🔧 多轮对话、消息发送后的 Bot 消息加入窗口上下文
- 🔧 回复分析提示词重写，模拟人类直觉判断

### 文档
- 📝 README 添加 AstrBot 仓库链接、QQ 群链接、AI 生成声明
- 📝 配置说明标注版本升级会覆盖提示词
- 📝 metadata.yaml 添加依赖声明

## [1.0.0] - 2025-05-08

### 新增
- 🎉 首次发布
- 双模式主动接话：**回复模式**（Bot 发言后监听群友回复）和 **主动模式**（Bot 没发言时随机抽取消息参与讨论）
- LLM 智能分析：判断群友消息是否在回复 Bot，以及 Bot 是否应主动参与群聊话题
- 支持分离 Provider：分析用便宜的模型，生成用高质量模型
- 群白名单机制：支持按群自定义回复概率和主动概率
- 冷却机制：防止 Bot 过于频繁发言
- 自定义提示词：分析、生成、主动参与的 LLM 提示词均可自定义
- Token 用量统计面板：WebUI 实时查看各群 Token 消耗趋势
- 多平台支持：aiocqhttp / telegram / discord / lark / qq_official / dingtalk / kook / slack / mattermost / satori
