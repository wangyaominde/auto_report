# WorkLog — AI 自动工作日报 / 周报

自动记录你一天用了什么应用、干了什么，定时截屏交给多模态大模型"看懂"，每天 21 点生成日报、每周五生成周报。带原生 GUI + 系统托盘，支持 Windows / macOS。

核心引擎为**纯 Python 标准库**实现（零第三方依赖），GUI 仅需三个轻量库。

## 功能

- **自动采集**：每 5 秒记录前台窗口（应用 + 标题 + 停留时长），本地 SQLite 存储
- **截图视觉分析**：每 5 分钟截全屏，每 2 小时把截图交给视觉模型生成"阶段小结"，日报据此还原你实际做了什么，而不是只看应用名猜
- **日报 / 周报 / 月报**：每天 21:00 自动出日报，周五 21:05 出周报；关机漏报会在下次启动时自动补齐（最近 7 天）
- **GUI + 托盘**：仪表盘、报告浏览（Markdown 渲染）、一键重新生成；关窗即缩托盘
- **记忆库（越用越准）**：
  - 报告里不属于你的条目，鼠标勾掉 → 自动成为负面样本，重新生成即剔除同类内容
  - 个人工作档案：告诉模型哪些项目是你的、哪些内容（同事消息 / 会议投屏 / AI 生成内容）不算你的工作
  - 历史报告检索：生成时自动对齐过去报告的项目名与进展表述
- **真实性约束**：提示词硬规则——查看 ≠ 完成、屏幕上出现 ≠ 你做的、会议投屏默认按他人内容处理、不确定标"（待确认）"；自动检测会议时段并注入警告
- **隐私设计**：
  - 黑名单窗口：时长不记录，截图中对应窗口区域**自动打码**（其余屏幕内容保留，不漏工作）
  - 托盘一键"隐私模式"：暂停一切记录与截图
  - 所有数据仅存本地；截图默认 7 天自动清理；仅截图与文字摘要会发送给你自己配置的 LLM
- **多模型支持**：GUI 设置页预设 MiniMax / OpenAI / Anthropic / Kimi / DeepSeek，可自定义任意 OpenAI / Anthropic 兼容接口，带秒级"测试连接"

## 快速开始

```bash
# 1. 克隆后安装 GUI 依赖（引擎本身零依赖；Python ≥ 3.10）
pip install -r requirements.txt

# 2. 配置模型：复制 .env.example 为 .env 填 Key，或启动后在 GUI「设置」页配置
cp .env.example .env

# 3. 启动
# Windows：
powershell -ExecutionPolicy Bypass -File .\start-gui.ps1
# macOS（需在系统设置中授予终端「辅助功能」与「屏幕录制」权限）：
python3 worklog_gui.py
```

启动后 GUI 常驻托盘，内置调度器自动完成一切：09:00–21:00 采集守护、5 分钟截图、2 小时阶段分析、21:00 日报、周五周报、启动补漏。**托盘"退出" = 自动化全部停止**，平时关窗即可。

> 开机自启：把 `start-gui.ps1` 的快捷方式放进 `shell:startup`（Windows），或将 `worklog_gui.py` 加入 macOS 登录项。

## 无 GUI 用法（引擎 CLI，零依赖）

```bash
python3 claude_auto_report_code_minimax.py                  # 前台采集，Ctrl+C 停止并出日报
python3 claude_auto_report_code_minimax.py --screenshot     # 截一张全屏（黑名单窗口自动打码）
python3 claude_auto_report_code_minimax.py --analyze-phase  # 分析新增截图 → 阶段小结
python3 claude_auto_report_code_minimax.py --report [YYYY-MM-DD]         # 日报（默认今天）
python3 claude_auto_report_code_minimax.py --weekly-report [YYYY-MM-DD]  # 该日所在周的周报
python3 claude_auto_report_code_minimax.py --monthly-report [YYYY-MM]    # 月报
python3 claude_auto_report_code_minimax.py --test-llm       # 测试 LLM 连接（秒级）
```

## 配置（`.env`）

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_API_URL` | 接口地址 | MiniMax Anthropic 兼容地址 |
| `LLM_API_KEY` | API Key（必填） | — |
| `LLM_MODEL` | 模型名（截图分析需支持视觉） | `MiniMax-M3` |
| `LLM_API_FORMAT` | `anthropic` / `openai`，留空按 URL 自动判断 | 自动 |
| `LLM_MAX_TOKENS` | 单次生成上限 | `8000` |
| `LLM_TEMPERATURE` | 低温度减少模型自由发挥 | `0.2` |
| `LLM_MAX_IMAGES` | 每段视觉分析发送的截图数 | `8` |
| `SCREENSHOT_RETENTION_DAYS` | 截图本地保留天数 | `7` |

## 工作原理

```
窗口采集(5s) ──┐
               ├─→ SQLite ──┐
截屏(5min) ────┘            ├─→ 阶段小结(每2h, 视觉模型) ─→ 日报(21:00) ─→ 周报(周五)
                            │         ↑
        记忆库(档案/修正/历史检索) ────┘  ← GUI 勾选「不是我做的」持续反馈
```

三层准确性防线：**采集层**（黑名单不记录 + 截图打码 + 隐私模式）→ **分析层**（会议检测、归属判别规则）→ **生成层**（记忆注入、真实性硬规则、低温度）。

## 目录说明

| 路径 | 内容 | 入库 |
|------|------|:---:|
| `claude_auto_report_code_minimax.py` | 核心引擎：采集 / 截图 / 分析 / 出报（纯标准库） | ✅ |
| `worklog_gui.py` + `ui/` | GUI + 托盘 + 内置调度器（pywebview / pystray / Pillow） | ✅ |
| `worklog_memory.py` | 记忆库 / 黑名单 / 隐私开关（纯标准库） | ✅ |
| `blacklist.json` | 黑名单关键词默认值（GUI 可维护） | ✅ |
| `reports/` `analysis/` `screenshots/` | 你的报告 / 小结 / 截图 | ❌ 仅本地 |
| `memory/` | 工作档案 + 修正记录 | ❌ 仅本地 |
| `.env` | API Key 等配置 | ❌ 仅本地 |

## 隐私声明

本工具会截取你的屏幕并发送给**你自己配置的** LLM 服务商用于生成工作摘要，请自行确认所用服务商的数据政策。所有原始数据（活动记录、截图、报告、记忆库）只存在你的本机，`.gitignore` 已确保它们不会被提交。
