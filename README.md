# WorkTime Tracker

Windows 前台工作时长统计工具，专注于追踪 Devin、IntelliJ IDEA、Codex、Unity Editor 等开发工具的实际使用时间。

## 功能

- **前台窗口检测**：通过 Win32 API 实时获取当前焦点窗口所属进程，仅统计前台活跃时间
- **空闲检测**：鼠标键盘无操作超过阈值（默认 5 分钟）自动暂停计时
- **系统托盘常驻**：关闭窗口时最小化到托盘，后台持续运行
- **数据持久化**：SQLite 本地存储，支持历史查询
- **可视化图表**：
  - 仪表盘：今日总时长卡片 + 饼图占比 + 应用明细表
  - 历史：7/14/30 天趋势折线图 + 区间汇总表
- **自定义进程**：可自由添加/移除监控的进程
- **CSV 导出**：支持按日期范围导出数据

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行
python main.py
```

## 默认监控进程

| 进程名 | 显示名 |
|--------|--------|
| `Devin.exe` | Devin |
| `idea64.exe` | IntelliJ IDEA |
| `idea.exe` | IntelliJ IDEA |
| `ChatGPT.exe` | Codex |
| `Unity.exe` | Unity Editor |

可在 Settings 页面添加其他进程（如 `devenv.exe` → Visual Studio、`Code.exe` → VS Code 等）。

## 数据存储位置

- 配置文件：`~/.worktime-tracker/config.json`
- 数据库：`~/.worktime-tracker/worktime.db`

## 技术栈

- **Python 3.10+**
- **PySide6** — Qt UI 框架
- **pyqtgraph** — 图表绘制
- **psutil + pywin32** — 进程/窗口检测
- **SQLite** — 本地数据持久化

## 打包为 EXE

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --icon=icon.ico main.py
```

## Codex Hook 集成

WorkTime Tracker 内置一个仅监听本机回环地址的 HTTP 接口，用于接收 Codex（或其他工具）发送的活动事件，自动按项目统计有效工作时长。

### 接口信息

- **地址**：`POST http://127.0.0.1:17890/events`
- **绑定**：仅 `127.0.0.1`，不对外网开放
- **健康检查**：`GET http://127.0.0.1:17890/health`

### 请求格式

```json
{
  "event": "PreToolUse",
  "sessionId": "abc-123",
  "project": "D:\\Data\\unity\\P1-c",
  "observedAt": "2026-07-20T12:00:00.000Z"
}
```

### 事件类型

| 事件 | 说明 |
|------|------|
| `SessionStart` | 会话开始，视为活动心跳 |
| `UserPromptSubmit` | 用户提交输入，视为活动心跳 |
| `PreToolUse` | 工具调用前，视为活动心跳 |
| `Stop` | 会话结束，结束当前活动段 |

### 计时规则

- 相邻两次心跳的间隔 ≤ 5 分钟时计入工时，超过则视为闲置不计
- 单次心跳间隔最多计入 2 分钟，防止离开电脑时虚增
- 同一项目的多个并发 session 不重复计时，按项目合并
- 重复事件（相同 sessionId + event + observedAt）幂等处理
- 项目显示名默认使用路径最后一级目录名

### 隐私

- 不保存用户输入、命令、代码内容或完整对话
- 仅持久化事件元数据和累计时长

### Codex Hook 配置示例

在 Codex 的 Hook 配置中，向本接口发送事件：

```bash
curl -X POST http://127.0.0.1:17890/events \
  -H "Content-Type: application/json" \
  -d '{"event":"PreToolUse","sessionId":"abc","project":"D:\\Projects\\MyGame","observedAt":"2026-07-20T12:00:00.000Z"}'
```

### 运行测试

```bash
py -m unittest tests.test_codex_activity -v
```
