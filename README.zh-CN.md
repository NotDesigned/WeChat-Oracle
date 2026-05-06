# WeChat Oracle

本地优先的微信群聊归档与 agent 助手。

[English](README.md)

WeChat Oracle 通过 [WeFlow](https://github.com/hicccc77/WeFlow) 记录微信群消息，写入 SQLite，可选地在本地对图片做 OCR、对语音做 ASR，并让一个 LLM 驱动的 bot 在群里回答问题。bot 也可以在后台维护每个群的长期记忆。

项目面向个人 Windows 机器上的微信 PC。除非你显式把内容发送给 LLM 或视觉模型服务，消息数据、媒体、记忆和调试日志都会留在 `data/` 目录下。

## 功能

- 通过 WeFlow SSE 实时采集微信消息并写入 SQLite。
- 导入 WeFlow JSON 历史导出。
- 存储标准化消息、合并转发子消息、媒体路径、OCR/ASR 文本、命令运行记录、agent 记忆和审计轨迹。
- 通过 wx4py UI 自动化把回复发回微信群。
- 支持 `/find`、`/sum`、`/recent`、`/ask`、`/explain`、`/balance` 等 slash 命令。
- 被直接 @、被引用回复或概率触发时，支持自由形式的 agent 对话。
- 支持静默的 `lurk` 学习链路，更新 `group_memory` / `persona_drift`，但不在群里发消息。
- agent turn 可以使用本进程 native tool loop，也可以委托给 OpenClaw runtime。

## 架构

```text
WeChat PC
   |
   v
WeFlow HTTP API
   |                         optional local worker
   | SSE live messages       OCR / ASR
   v                         |
ingest live -----------------+
   |
   v
SQLite WAL database: data/wechat-oracle.db
   |
   +--> dispatcher --> LLM / agent backend --> wx4py reply to WeChat
   |
   +--> agent lurk --> memory writes only, no reply
```

生产环境通常常驻两个进程：

```powershell
uv run wechat-oracle ingest live
uv run wechat-oracle dispatcher
```

`ingest live` 启动 SSE 订阅，并内嵌一个 mm worker 线程。`dispatcher` 轮询 SQLite，处理显式命令和 agent 触发，并把所有 wx4py 发送操作串行化到一个发送线程，保证同一时间只有一个 GUI 操作触碰微信。

## 环境要求

- Windows 10/11。
- 微信 PC 4.1.x Qt 版。wx4py 回复路径依赖可见的桌面 UI。
- 已启用 HTTP API 的 WeFlow 桌面端。
- Python 3.12+。
- [uv](https://docs.astral.sh/uv/)。
- OpenAI-compatible LLM endpoint，或用于 `WO_AGENT_BACKEND=openclaw` 的 OpenClaw local gateway。

初始化数据库、历史导入、状态检查和 WeFlow 诊断等非回复流程不需要 wx4py。发回微信群需要微信主窗口可见，不能最小化到托盘。

## 快速开始

```powershell
git clone https://github.com/<your-account>/WeChat-Oracle.git
cd WeChat-Oracle
uv sync
uv run wechat-oracle init-db
```

在仓库根目录创建 `.env`。先写公共配置：

```env
# WeFlow
WO_WEFLOW_TOKEN=<weflow-token>
WO_GROUPS=

# Bot identity in the target group
WO_BOT_NAME=<bot-group-nickname>

# Reply path
WO_REPLY=True
WO_REPLY_BACKEND=wx4py

# Optional agent tuning
WO_AGENT_BASE_PROBABILITY=0.25
WO_AGENT_RECENT_CONTEXT_CHAT=100
WO_LLM_MAX_TOKENS=5000
WO_LLM_WRITE_MAX_TOKENS=10000

# Optional background learning
WO_AGENT_LURK_ENABLED=False
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

然后选择一个 agent backend。

选项 A：直接使用普通 OpenAI-compatible API：

```env
WO_AGENT_BACKEND=native
WO_LLM_API_KEY=<api-key>
WO_LLM_ENDPOINT=https://api.deepseek.com
WO_LLM_MODEL=deepseek-v4-pro
```

选项 B：使用 OpenClaw 作为 agent runtime：

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
```

然后运行：

```powershell
# Terminal 1: live ingest + OCR/ASR worker
uv run wechat-oracle ingest live

# Terminal 2: command/agent dispatcher
uv run wechat-oracle dispatcher
```

在已监控的群里发送：

```text
@<bot-name> /help
@<bot-name> 今天谁提到了股票？
@<bot-name> /find since:2026-05 关于装修
```

如果 `WO_GROUPS` 为空，live ingest 会监控 WeFlow sessions 当前暴露的所有群聊。也可以把 `WO_GROUPS` 设为逗号分隔的群名、备注或 wxid 列表。

## 核心命令

通用 CLI：

```powershell
uv run wechat-oracle init-db
uv run wechat-oracle status
uv run wechat-oracle ingest live
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle dispatcher
uv run wechat-oracle worker mm
```

WeFlow 诊断：

```powershell
uv run wechat-oracle weflow find <group-name-or-wxid-fragment>
uv run wechat-oracle weflow sessions --groups-only
```

Agent 状态：

```powershell
uv run wechat-oracle agent show <group_id>
uv run wechat-oracle agent show-runs <group_id> -n 10
uv run wechat-oracle agent lurk <group_id>
uv run wechat-oracle agent wipe <group_id>
uv run wechat-oracle agent wipe <group_id> --persona-only
uv run wechat-oracle agent wipe <group_id> --memory-only -y
```

健康检查：

```powershell
uv run wechat-oracle verify roundtrip
```

OpenClaw runtime 辅助命令：

```powershell
uv run wechat-oracle openclaw ping
uv run wechat-oracle openclaw mcp-test
uv run wechat-oracle openclaw mcp-serve
```

`openclaw mcp-serve` 是给 OpenClaw 拉起的 stdio MCP server 入口。`openclaw ping` 用来测试已配置的 OpenClaw chat-completions gateway。

## 历史消息回灌

backfill 会把 WeFlow 历史导出导入 live ingest 使用的同一个 SQLite 归档：

```powershell
uv run wechat-oracle ingest backfill <export.json> --format weflow
```

这个命令一次导入一个导出文件。如果有多个 session 导出文件，需要逐个运行。重复导入同一个文件是安全的：所有导入消息都会走 `ingest/writer.py:write_messages()`，并通过 `UNIQUE(dedupe_key)` 跳过重复消息。

支持的格式：

| 格式 | 输入 |
|---|---|
| `weflow` | WeFlow JSON 导出，顶层包含 `session` 和 `messages` 字段。 |
| `jsonl` | 每行一个 canonical `Message` JSON 对象，主要用于管道测试或自定义 importer。 |

对 WeFlow 导出，importer 会：

- 从 `session.wxid` 得到 `group_id`，缺失时回退到文件名 stem。
- 从 `session.displayName`、`session.nickname` 或 `session.remark` 得到 `group_name`。
- 把 WeFlow `localType` 转成标准消息类型：text、image、voice、video、sticker、link、quote、forward、system。
- 用 `reply_to_wx_msg_id` 和 `quote_text` 保留引用回复关系。
- 把合并转发消息解析到 `forwarded_records`，因此 `/find`、`/sum` 和 agent 历史工具可以搜索合并转发内部内容。
- 把导出目录里引用到的本地媒体复制到 `data/media/<group_id>/<kind>/`，并在 DB 中保存相对 `data/` 的 `media_path`。

如果导出里引用的媒体文件缺失，消息仍会导入，但会写入一个媒体缺失占位符。OCR/ASR 只能处理已经存在于 `data/media` 下的媒体文件。

backfill 行会使用 `source='backfill'`。dispatcher 只会被新的 `source='live'` 行唤醒，所以导入旧消息不会让 bot 回复历史 @。但这些历史消息仍然可被 `/find`、`/sum`、`/recent`、agent 历史召回，以及手动或自动 `lurk` 学习使用。

## 群内命令

dispatcher 接受 `@<bot> /cmd ...`，也接受群里单独发送的 `/cmd ...`。

| 命令 | 用途 |
|---|---|
| `/find [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] <query>` | 在当前群归档里做语义搜索。 |
| `/sum [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] [limit:N] [topic]` | 总结当前群消息。 |
| `/recent [N]` | 不调用 LLM，直接显示最近入库消息。 |
| `/ask <question>` | 不带群历史上下文的轻量 LLM 调用。 |
| `/explain [text]` | 解释引用消息或给定文本。 |
| `/balance` | 查询已配置的 DeepSeek-compatible 余额接口。 |
| `/help [command]` | 显示命令帮助。 |

示例：

```text
@小号 /find from:张三 since:2026-05 关于装修
@小号 /sum limit:100 昨晚讨论了什么
@小号 /recent 20
@小号 /ask SQLite WAL 是什么？
引用一张图片后发送：@小号 /explain
```

## Agent 行为

系统有三条独立运行链路。

### 1. 采集链路

`ingest live`、`ingest backfill` 和 `worker mm` 只把标准化数据写入 SQLite。它们不决定 bot 是否应该说话。

### 2. 交互链路

dispatcher 会分类每条新的 live 消息：

| 触发 | 条件 | 行为 |
|---|---|---|
| `mention` | 文本包含真实的 `@<WO_BOT_NAME>`。 | 总是唤醒 bot。Slash 命令会被解析，普通文本进入 agent chat。 |
| `reply` | 消息引用回复了 bot 之前的消息，且 `WO_BOT_WXID` 已知或可自动发现。 | 总是唤醒 bot。 |
| `probability` | 不是 mention/reply，通过类型门槛、随机阈值和 cooldown。 | agent 自己决定说话或 `stay_silent`。 |
| none | 没有触发。 | 不调用 LLM，消息标记为 no-trigger。 |

直接 @ 和引用回复 bot 会直接运行 agent。只有当 agent 返回非空回复时，bot 才会在群里发送消息。

native agent 分两阶段：

- Phase A 读取最近上下文，可调用只读工具，最后返回群聊回复或 `stay_silent`。
- Phase B 读取 Phase A trace，可更新 `group_memory` 或 `persona_drift`。

Phase A 初始上下文包含当前群最近 `WO_AGENT_RECENT_CONTEXT_CHAT` 条消息。工具可以搜索更早历史、展开引用链、展开合并转发、读取 OCR/ASR 文本、通过视觉模型读图、读取语音转写，或读取群记忆。

### 3. 后台学习链路

`agent lurk <group_id>` 和可选 auto-lurk 会读取带水位的新增消息批次并更新记忆，但不会回复。

- 首次运行读取最近 `WO_AGENT_LURK_RECENT_MSGS` 条消息。
- 后续运行从 `agent_lurk_state.last_msg_id` 继续。
- lurk agent 可以在新批次指向旧上下文时调用历史工具查看老消息。
- 它只把稳定、可复用的信息写入 `group_memory`，或把长期行为调整写入 `persona_drift`。
- 它不会调用 wx4py，也不会发送收到提示。

在 dispatcher 中启用自动 lurk：

```env
WO_AGENT_LURK_ENABLED=True
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

auto-lurk 使用单独的单 worker，不占用聊天响应 worker。

## Agent Backends

`WO_AGENT_BACKEND=native` 是默认值。它通过 OpenAI-compatible LLM endpoint 在当前进程内运行 tool-calling loop。

`WO_AGENT_BACKEND=openclaw` 会把 chat 和 lurk turn 委托给 OpenClaw agent，通过本地 OpenAI-compatible gateway 调用。OpenClaw 可以使用本项目的 MCP server 调 WeChat-Oracle 工具。如果生产 bot 想复用已有订阅型 agent runtime，这条路径更合适。

两种模式只由一个配置项切换：

```env
# Use the ordinary OpenAI-compatible API directly.
WO_AGENT_BACKEND=native

# Or use OpenClaw as the agent runtime.
WO_AGENT_BACKEND=openclaw
```

注意：这里的 OpenClaw 是 agent runtime backend，不是微信群发 backend。群回复仍然通过 wx4py 或 `stdout`。

OpenClaw 相关配置：

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
```

## 真实微信 @

`Wx4pyReplier` 会尝试创建真实的微信群 @ token：打开群聊、输入 `@`、从微信候选弹窗里选择提问者，然后粘贴回复正文。如果检测不到候选人，会回退为纯文本 `@name` 并记录 warning。

这一路径会通过 `_SerialReplier` 串行化；多个 worker 线程不会同时操作微信 GUI。

## 多媒体

mm worker 会填充 `messages.transcript`：

- 图片和表情：通过 `rapidocr-onnxruntime` 本地 OCR。
- 语音：通过 `faster-whisper` 本地 ASR。

transcript 状态：

- `NULL`：尚未处理。
- `''`：已处理，但没有识别出文字。
- 非空文本：可用的 OCR/ASR 结果。

如果配置了 `WO_VISION_API_KEY`，agent chat 和 `/explain` / `/ask` 可以把被引用的图片发送给视觉模型直接读取。否则图片处理会回退到 OCR 文本和占位符。

## 数据模型

关键表：

| 表 | 用途 |
|---|---|
| `messages` | 标准化微信消息。 |
| `forwarded_records` | 合并转发消息里的子消息。 |
| `command_runs` | dispatcher 幂等与命令状态。 |
| `group_state` | live/backfill 的群级游标。 |
| `persona_drift` | 每群可演化的行为补充。 |
| `group_memory` | 每群自由文本长期记忆文档。 |
| `agent_run_log` | agent 审计轨迹。 |
| `agent_lurk_state` | lurk 游标，独立于审计日志。 |

所有导入消息的写入路径都走 `ingest/writer.py:write_messages()`，并依赖 `UNIQUE(dedupe_key)` 做跨源去重。

`group_memory` 和 `persona_drift` 是整文档替换写入。写工具会强制 agent 先读取当前状态再写，并检查中间状态没有变化。如果另一个并发 agent run 先更新了记忆，工具会要求模型重新读取并合并。

## 配置参考

所有运行时配置都使用 `WO_` 前缀，可以写在 `.env` 或进程环境变量里。

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `WO_DATA_DIR` | `data` | 数据根目录。 |
| `WO_DB_PATH` | `data/wechat-oracle.db` | SQLite 数据库路径。 |
| `WO_MEDIA_DIR` | `data/media` | 媒体目录。 |
| `WO_GROUPS` | `[]` | 空表示所有 WeFlow 群会话；接受逗号字符串或 JSON list。 |
| `WO_LOG_LEVEL` | `INFO` | loguru level。 |
| `WO_WEFLOW_BASE_URL` | `http://127.0.0.1:5031` | WeFlow HTTP API root。 |
| `WO_WEFLOW_TOKEN` | empty | WeFlow token。 |
| `WO_WEFLOW_POLL_INTERVAL` | `30.0` | 已废弃；live 使用 SSE。 |
| `WO_BOT_NAME` | empty | bot 在群里的昵称。dispatcher 必填。 |
| `WO_BOT_WXID` | empty | 可选 bot wxid，用于 reply-to-bot 触发。 |
| `WO_LLM_PROVIDER` | `openai-compatible` | LLM provider adapter。 |
| `WO_LLM_API_KEY` | empty | LLM API key。 |
| `WO_LLM_ENDPOINT` | `https://api.deepseek.com` | OpenAI-compatible endpoint。 |
| `WO_LLM_MODEL` | `deepseek-v4-pro` | 模型名。 |
| `WO_LLM_JSON_MODE` | `native` | `native` 或 `prompt` JSON mode。 |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | DB 轮询间隔，单位秒。 |
| `WO_DISPATCHER_WORKER_THREADS` | `4` | 全局消息 worker；wx4py 发送仍然串行。 |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` 候选上限。 |
| `WO_DISPATCHER_CONTEXT_CHAT` | `2500` | 旧聊天上下文上限，部分总结路径仍使用。 |
| `WO_LLM_MAX_TOKENS` | `5000` | 通用输出上限。 |
| `WO_LLM_CHAT_MAX_TOKENS` | empty | 覆盖 chat 输出上限。 |
| `WO_LLM_SUM_MAX_TOKENS` | empty | 覆盖 summary 输出上限。 |
| `WO_LLM_SHORT_MAX_TOKENS` | empty | 覆盖 `/ask` / `/explain` 输出上限。 |
| `WO_LLM_WRITE_MAX_TOKENS` | `10000` | memory-write tool call 输出上限。 |
| `WO_VISION_PROVIDER` | `openai-compatible` | Vision adapter。 |
| `WO_VISION_API_KEY` | empty | 启用直接读图。 |
| `WO_VISION_ENDPOINT` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Vision endpoint。 |
| `WO_VISION_MODEL` | `qwen-vl-plus` | Vision 模型。 |
| `WO_VISION_MAX_IMAGES` | `3` | 每次 vision 请求的图片上限。 |
| `WO_VISION_MAX_TOKENS` | `800` | Vision 输出上限。 |
| `WO_AGENT_BASE_PROBABILITY` | `0.25` | 概率触发阈值；设为 `0` 表示只响应显式触发。 |
| `WO_AGENT_COOLDOWN_SECONDS` | `30` | 每群 probability cooldown。 |
| `WO_AGENT_MAX_STEPS` | `5` | Native Phase A 最大轮数。 |
| `WO_AGENT_REFLECT_MAX_STEPS` | `3` | Native Phase B 最大轮数。 |
| `WO_AGENT_REFLECTION_ENABLED` | `True` | 启用 chat Phase B 记忆反思。 |
| `WO_AGENT_PERSONAS_DIR` | `data/personas` | Persona YAML 目录。 |
| `WO_AGENT_RECENT_CONTEXT_CHAT` | `100` | agent chat 初始最近消息窗口。 |
| `WO_AGENT_MEMORY_MAX_CHARS` | `100000` | `group_memory` 硬字符上限。 |
| `WO_AGENT_MAX_TOOL_CALLS_PER_RUN` | `12` | Native Phase A tool 总预算。 |
| `WO_AGENT_MAX_TOOL_CALLS_PER_STEP` | `4` | Native Phase A 单 step tool 预算。 |
| `WO_AGENT_MAX_IMAGE_READS_PER_RUN` | `2` | Native 读图预算。 |
| `WO_AGENT_MAX_VOICE_READS_PER_RUN` | `2` | Native 读语音预算。 |
| `WO_AGENT_LURK_ENABLED` | `False` | 启用 dispatcher auto-lurk。 |
| `WO_AGENT_LURK_INTERVAL_SECONDS` | `1800` | auto-lurk 扫描间隔。 |
| `WO_AGENT_LURK_MIN_NEW_MESSAGES` | `20` | 触发 auto-lurk 的最少新增消息数。 |
| `WO_AGENT_LURK_RECENT_MSGS` | `100` | 单次 lurk 最多处理消息数。 |
| `WO_AGENT_LURK_MAX_STEPS` | `4` | Lurk tool-calling 轮数。 |
| `WO_OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway。 |
| `WO_OPENCLAW_TOKEN` | empty | OpenClaw gateway token。 |
| `WO_OPENCLAW_AGENT_ID` | `wechat-bot` | OpenClaw agent id。 |
| `WO_AGENT_BACKEND` | `native` | `native` 或 `openclaw`。 |
| `WO_REPLY` | `True` | 是否把回复发回微信。 |
| `WO_REPLY_BACKEND` | `wx4py` | `wx4py` 或 `stdout`。 |

`WO_WHISPER_MODEL` 由 mm worker 直接读取，默认值为 `small`；可接受值取决于 faster-whisper，常见为 `tiny`、`base`、`small`、`medium`、`large-v3`。

## 日志与调试

- `data/dispatcher.log`：紧凑的人类可读命令与 agent trace。
- `data/llm_debug.log`：LLM prompt、原始回复、解析结果和完整 trace。
- `wx4py_send_audit.jsonl`：wx4py 产生的发送审计记录。

常用检查：

```powershell
uv run wechat-oracle status
uv run wechat-oracle verify roundtrip
uv run wechat-oracle agent show-runs <group_id> -n 20
```

`verify roundtrip` 用来确认 WeFlow 是否会把 bot 自己通过 wx4py 发出的消息回流到 `messages`。这个回流是 bot wxid 自动发现和 reply-to-bot 触发所必需的。

## 已知限制

- wx4py 依赖可见的 Windows 桌面和微信 Qt UI。它不是官方微信 API。
- 群回复发送只实现了 `wx4py` 和本地 `stdout`。
- OpenClaw 支持的是 agent runtime backend，不是群消息发送 backend。
- 如果 WeFlow 没有暴露历史媒体文件，backfill 导入的旧图片/语音无法 OCR/转写，除非你补齐媒体文件。
- 视频内容目前只存储 metadata/path，没有实现视频理解。
- WeFlow 缺少 `wx_msg_id` 时，去重是 best-effort。

## 开发

当前仓库没有 pytest、ruff、mypy 或 CI 测试套件。提交前建议运行：

```powershell
uv run python -m compileall src\wechat_oracle
uv run wechat-oracle agent --help
uv run wechat-oracle status
uv run python .Codex\hooks\check_doc_sync.py
git diff --check
```

每个 clone 启用一次 git hook：

```powershell
git config core.hooksPath .githooks
```

如果修改 schema、CLI 命令、配置字段或 dispatcher 命令语法，请在同一个 commit 里同步更新 README。

## License

MIT。见 [LICENSE](LICENSE)。
