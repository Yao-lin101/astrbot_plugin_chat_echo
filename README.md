# AstrBot 主动接话插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-AGPLv3-green)](LICENSE)

## 简介

一个智能的 AstrBot 群聊主动接话插件。Bot 发出消息后，自动监听后续群友的回复，使用 LLM 分析是否在跟 Bot 说话，如果是，则使用 LLM 生成自然回复并主动发言。

## 功能特点

### 双模式工作

| 模式 | 说明 |
|------|------|
| **回复模式** | Bot 发言后，跟踪窗口内群友消息，按概率分析是否回复 Bot |
| **主动模式** | Bot 没发言时，按概率随机抽取消息分析话题并主动参与讨论 |

### 核心特性

- ✅ **双路线互斥** — 回复模式和主动模式互不干扰
- ✅ **LLM 智能分析** — 判断群友消息是否在回复 Bot，以及 Bot 是否应主动参与话题
- ✅ **可配置概率** — 支持全局概率和按群自定义概率
- ✅ **群白名单** — 只在指定群启用，支持为每个群单独设置概率
- ✅ **冷却机制** — 防止 Bot 过于频繁发言
- ✅ **分离 Provider** — 分析用便宜的模型，生成用高质量模型
- ✅ **自定义提示词** — 分析、生成、主动参与的提示词均可自定义

## 配置说明

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `proactive_enabled` | bool | `false` | 启用主动接话功能 |
| `trigger_mode` | string | `llm_response` | 触发模式：`llm_response`（仅 LLM 回复后监听）/ `any_message`（任何消息后监听） |

### Provider 配置

| 配置项 | 说明 |
|--------|------|
| `analyzer_provider_id` | 分析用 LLM Provider（建议用便宜的模型）。留空则使用默认 Provider |
| `generator_provider_id` | 生成用 LLM Provider（建议用质量高的模型）。留空则使用默认 Provider |

### 跟踪与检测

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `track_timeout_seconds` | int | `120` | 跟踪窗口（秒），群聊沉默超过此时间即停止 |
| `max_detection_count` | int | `10` | 最多分析多少条群友发言后停止本轮 |

### 概率控制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `reply_probability` | int | `100` | 回复模式分析概率(%)，100=每条都分析 |
| `active_probability` | int | `0` | 主动模式参与概率(%)，0=关闭主动模式 |
| `active_interval_minutes` | int | `30` | 主动模式最小间隔(分钟) |

### 群白名单

通过 `enabled_groups` 配置，支持格式：
- `"群ID"` — 基本白名单
- `"群ID:回复概率"` — 自定义回复概率
- `"群ID:回复概率:主动概率"` — 自定义回复和主动概率

示例：
```json
["-100123456:80:30", "-100789012:50"]
```

### 其他

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_proactive_rounds` | int | `3` | 最大连续主动回复轮数 |
| `proactive_cooldown_seconds` | int | `300` | 主动发言冷却(秒) |

### 自定义提示词

支持自定义以下 LLM 提示词：
- `analyzer_system_prompt` — 回复分析提示词
- `proactive_analyzer_system_prompt` — 主动分析提示词
- `generator_system_prompt` — 回复生成提示词

## 安装

1. 在 AstrBot 管理面板中，进入「插件市场」
2. 搜索 `astrbot_plugin_chat_echo`
3. 点击安装
4. 或在 `data/plugins` 目录下执行：
   ```bash
   git clone https://github.com/AMYdd00/astrbot_plugin_chat_echo.git
   ```

## 使用

1. 在 AstrBot 管理面板中启用插件
2. 配置 `proactive_enabled` 为 `true`
3. 根据需要调整其他配置项
4. 在群聊中正常与 Bot 对话即可

## 工作原理

```
Bot 发言 → 开始跟踪窗口 → 群友回复 → LLM 分析是否回复 Bot
                                         ↓
                                    是 → 生成自然回复 → Bot 主动发言
                                         ↓
                                    否 → 继续跟踪/超时停止
```

## 开源协议

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 开源。

## 作者

- [AMYdd00](https://github.com/AMYdd00)
