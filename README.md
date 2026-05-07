# AstrBot 主动接话插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-AGPLv3-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blueviolet)](CHANGELOG.md)

## 简介

一个智能的 AstrBot 群聊主动接话插件。Bot 发出消息后，自动监听后续群友的回复，使用 LLM 分析是否在跟 Bot 说话，如果是，则使用 LLM 生成自然回复并主动发言。

插件自带 **Token 用量统计面板**，可在 AstrBot WebUI 中查看各群的 LLM Token 消耗趋势。

## 功能特点

### 双模式工作

| 模式 | 说明 |
|------|------|
| **回复模式** | Bot 发言后，跟踪窗口内群友消息，按概率分析是否回复 Bot |
| **主动模式** | Bot 没发言时，按概率随机抽取消息分析话题并主动参与讨论 |

### 核心特性

- ✅ **双路线互斥** — 回复模式和主动模式互不干扰
- ✅ **LLM 智能分析** — 判断群友消息是否在回复 Bot，以及 Bot 是否应主动参与话题
- ✅ **分离 Provider 支持** — 分析用便宜的模型（如 GPT-4o-mini），生成用高质量模型（如 GPT-4.1）
- ✅ **可配置概率** — 支持全局概率和按群自定义概率
- ✅ **群白名单** — 只在指定群启用，支持为每个群单独设置概率
- ✅ **冷却机制** — 防止 Bot 过于频繁发言（支持回复冷却和主动冷却独立配置）
- ✅ **自定义提示词** — 分析、生成、主动参与的 LLM 提示词均可自由定制
- ✅ **Token 用量统计** — 内置 WebUI 面板，按群/按天展示 Token 消耗趋势
- ✅ **多平台兼容** — 支持 aiocqhttp / telegram / discord / lark / qq_official / dingtalk / kook / slack / mattermost / satori

## 配置说明

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `trigger_mode` | string | `llm_response` | 触发模式：`llm_response`（仅 LLM 回复后监听）/ `any_message`（任何消息后监听） |

### Provider 配置

| 配置项 | 说明 |
|--------|------|
| `analyzer_provider_id` | 分析用 LLM Provider（建议用便宜的模型如 GPT-4o-mini）。留空则使用会话的默认 Provider |
| `generator_provider_id` | 生成用 LLM Provider（建议用质量高的模型如 GPT-4.1）。留空则使用会话的默认 Provider |

### 跟踪与检测

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `track_timeout_seconds` | int | `120` | 跟踪窗口（秒）。群聊沉默超过此时间即停止本轮监听。范围 10~600 |
| `max_detection_count` | int | `10` | 最多分析多少条群友发言后停止本轮。与跟踪窗口是「或」关系，任一先触发则停止 |

### 概率控制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `reply_probability` | int | `100` | 回复模式分析概率(%)。100=每条都分析，50=一半概率 |
| `active_probability` | int | `0` | 主动模式参与概率(%)。0=关闭主动模式，100=每条都参与分析 |
| `active_interval_minutes` | int | `30` | 主动模式最小间隔(分钟)。防止主动参与过于频繁 |

### 群白名单

通过 `enabled_groups` 配置。

**格式：`群标识:【Bot发言后发给LLM分析要不要回复的概率】:【主动随机发给LLM分析要不要参与聊天的概率】`**

可留空某个值，如 `群ID::30` 表示只设主动概率、回复概率用全局值。

UMO格式也支持：`Bot:GroupMessage:群ID`

示例：
```json
["-100123456:80:30", "Bot:GroupMessage:-100789012:50"]
```

- `"-100123456:80:30"` — 群号`-100123456`，回复概率80%，主动概率30%
- `"-100789012:50"` — 群号`-100789012`，回复概率50%，主动概率用全局值
- `"-100123456::30"` — 群号`-100123456`，回复概率用全局值，主动概率30%
- `"Bot:GroupMessage:-100789012:50"` — UMO格式（Bot:GroupMessage:群ID），回复概率50%
- `"-100789012"` — 仅白名单，两个概率都用全局值

> 留空则所有群使用全局概率。

### 其他

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_proactive_rounds` | int | `3` | 最大连续主动回复轮数。Bot 回复后自动开启新一轮监听，1 表示只接话一次不继续 |
| `proactive_cooldown_seconds` | int | `300` | 主动发言冷却(秒)。每次主动发言后需等待多久才能再次在该群触发新的跟踪窗口 |

### 自定义提示词

支持自定义以下 LLM 提示词，可通过 AstrBot WebUI 配置界面编辑：

| 配置项 | 说明 | 默认用途 |
|--------|------|----------|
| `analyzer_system_prompt` | 回复分析提示词 | 判断群友消息是否在回复 Bot |
| `proactive_analyzer_system_prompt` | 主动分析提示词 | 判断 Bot 是否应该主动参与讨论 |
| `generator_system_prompt` | 回复生成提示词 | 生成 Bot 的自然回复内容 |

## 安装

### 方法一：插件市场安装

1. 打开 AstrBot WebUI 管理面板
2. 进入「插件市场」
3. 搜索 `astrbot_plugin_chat_echo`
4. 点击安装

### 方法二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/AMYdd00/astrbot_plugin_chat_echo.git
```

## 使用

1. 在 AstrBot 管理面板中启用插件
2. 根据需要调整其他配置项（建议至少配置 `analyzer_provider_id` 和 `generator_provider_id`）
4. 在群聊中正常与 Bot 对话即可

### Token 统计面板

启用插件后，在 AstrBot WebUI 中可通过以下路径访问 Token 用量统计：
- `插件页面 → Token 用量统计`
- 支持按日/周/月/年/全部维度的数据查看
- 展示各群的 Token 消耗排行和趋势图

## 工作原理

```
Bot 发言 → 开始跟踪窗口 → 群友回复 → LLM 分析是否回复 Bot
                                         ↓
                                    是 → LLM 生成自然回复 → Bot 主动发言
                                         ↓
                                    否 → 继续跟踪 / 超时停止

同时：随机消息 → 主动概率命中 → LLM 分析是否参与 → 是 → 生成发言
```

## 技术细节

- **路由互斥**：回复模式（Route 1）和主动模式（Route 2）使用互斥锁，避免同时触发冲突
- **Token 统计**：每次 LLM 调用自动记录 Prompt/Completion Token 用量，按群按天聚合，自动清理超过 365 天的旧数据
- **群名缓存**：群名自动捕获并持久化，Token 统计面板可直接显示群名

## 开源协议

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 开源。

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)

## 作者

- [AMYdd00](https://github.com/AMYdd00)
