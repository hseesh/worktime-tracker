# WorkTime Tracker

Windows 本地工作时长统计工具。它按显示器采样当前可见窗口，支持同时统计主屏和副屏上的不同工具，并通过窗口标题、项目名和自定义规则区分 Work、Indie 等标签。

## 当前计时口径

- 默认每 1 秒采样一次，使用单调时钟计算两次采样之间的真实间隔。
- 每个显示器最多选择一个窗口：焦点所在显示器使用焦点窗口，其他显示器优先选择面积最大的受监控可见窗口。
- 多个显示器并行累计。例如主屏 Devin Work、副屏 Devin Indie 同时显示 1 小时，总应用时长会增加 2 小时。
- 未配置的前台/可见应用仍会记录为 `Other`。
- 默认连续 5 分钟没有键鼠输入后进入 Idle；达到阈值前的 5 分钟仍归入当时的应用。
- 暂停计时后不再产生应用或 Idle 记录。

## Devin 多窗口

Devin 支持在不同显示器全屏打开不同项目。工具从窗口标题中解析 workspace，并以项目作为独立计时维度：

```text
Assets - Devin - ...    -> Devin [Assets]    -> Indie
zs-cloud - Devin - ...  -> Devin [zs-cloud]  -> Work
```

标题同时支持 `-`、`–`、`—` 分隔符。每个显示器上的 Devin 窗口分别写入秒级片段，因此两个项目可以同时增长，不会合并为一个 `Devin.exe` 总数。

项目/标签判断优先级：

1. App Breakdown 中针对该应用/项目设置的 Tag 覆盖。
2. 窗口标题关键词规则，按配置顺序首个命中。
3. 进程默认标签。
4. 没有规则时归入 `Other`。

App Breakdown 的下拉框会保存精确的“进程 + 项目”覆盖；没有项目的普通应用使用“进程 + 显示名”。设置后会同步更新已有秒级片段，且后续采样不会再被关键词规则改回。两个 Devin 项目可以分别设置，不会互相覆盖。

## Codex Hook

Codex 桌面端按单实例处理。Hook 只告诉计时器“当前 Codex 属于哪个项目”，不会根据两次 Hook 的时间间隔直接增加工时；实际秒数只在 Codex 窗口可见且被显示器采样选中时累计。

- 事件接口：`POST http://127.0.0.1:17890/events`
- 健康检查：`GET http://127.0.0.1:17890/health`
- 支持事件：`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`
- 心跳项目在最后一次事件后保持活跃 5 分钟，`Stop` 可提前结束。
- 只使用最近活跃项目作为当前 Codex 项目。
- 重复的 `sessionId + event + observedAt` 事件幂等处理。
- 项目显示名默认使用路径最后一级目录名，再根据 Indie 关键词添加分类。

请求示例：

```json
{
  "event": "PreToolUse",
  "sessionId": "abc-123",
  "project": "D:\\Data\\unity\\P1-c",
  "observedAt": "2026-07-20T12:00:00.000Z"
}
```

`codex_hook.py` 可接收 Codex stdin JSON，并转发到本地事件接口。工具只保存事件类型、session ID、项目路径和时间，不保存输入内容、命令、代码或对话正文。

## Chrome URL 跟踪

Chrome 窗口标题是动态的（如 ChatGPT 对话主题），不适合关键词分类。本工具通过一个轻量 Chrome 扩展上报当前标签页 URL，再按域名规则判断 Work/Indie 标签。

### 工作原理

1. Chrome 扩展（`chrome-extension/`）监听标签切换/窗口聚焦，POST 当前 URL 到 `http://127.0.0.1:17891/api/chrome-url`
2. Tracker 缓存最新 URL（120 秒过期）
3. 采样时对 `chrome.exe` 窗口优先用 URL 域名规则判断标签
4. **Chrome 没有命中任何 URL/关键词规则时不计时**（不会 fallback 到默认标签）

### 安装扩展

1. Chrome 打开 `chrome://extensions`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展程序」，选择 `chrome-extension/` 目录

### 默认域名规则

| 域名 | 标签 |
|---|---|
| `chatgpt.com` | Indie |
| `claude.ai` | Indie |
| `gemini.google.com` | Indie |
| `itch.io` | Indie |
| `unity.com` | Indie |
| `flowus.cn` | Indie |
| `gitee.com` | Work |
| `github.com` | Work |

域名规则支持后缀匹配（如 `unity.com` 匹配 `assetstore.unity.com`）。可在 Settings 页面的 `url_tag_rules` 中自定义。

### Event Log

Chrome URL 上报记录会写入 `chrome_url_events` 表，可在 Event Log 中通过 `filter=chrome` 查看。

## History 日历热力图

History 页面顶部提供最近 365 天的日历热力图：

- 默认按 Indie 时间着色，也可以切换为 Work 或 Total。
- 每个方块代表一个本地自然日，颜色越深表示对应指标越多。
- 点击日期会展开当天的总计、Idle、Tag 分布、App/Project 明细和小时时间轴。
- 热力图不计入 Idle；当天详情会单独展示 Idle。

## 数据与统计

- 秒级事实表：`time_segments`
- 应用兼容汇总：`time_records`
- Codex 事件：`codex_events`
- Codex 项目兼容汇总：`codex_time_records`

今日看板、历史趋势、标签统计和 CSV 导出统一从 `time_segments` 聚合，均排除 Idle。CSV 包含日期、应用、进程、项目、标签、秒数和最后更新时间，也包含 Codex 项目时间。

存储位置：

- 配置：`~/.worktime-tracker/config.json`
- 数据库：`~/.worktime-tracker/worktime.db`

## 运行

推荐直接运行：

```bat
start.bat
```

手动运行：

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

服务启动后：

- Web UI：`http://127.0.0.1:17891`
- Codex Hook：`http://127.0.0.1:17890/events`

程序默认写入当前用户的 Windows 启动项，并常驻系统托盘。相关选项可在 Settings 页面调整。

## 默认监控进程

| 进程名 | 显示名 |
|---|---|
| `Devin.exe` | Devin |
| `idea64.exe` / `idea.exe` | IntelliJ IDEA |
| `ChatGPT.exe` | Codex |
| `Unity.exe` | Unity Editor |
| `Weixin.exe` / `WeChat.exe` / `WeChatAppEx.exe` | WeChat (Work) |
| `msedge.exe` | Edge |
| `chrome.exe` | Chrome |

可以在 Settings 页面增加、移除进程，配置进程默认标签和窗口标题关键词规则。

## 技术栈

- Python 3.10+
- Flask Web API
- Vue 3 + ECharts Web UI
- pystray + Pillow 系统托盘
- psutil + pywin32 窗口检测
- SQLite 本地持久化

## 测试

```bash
python -m unittest discover -s tests -v
```
