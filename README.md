# WeChat Oracle

Local-first WeChat group archive and agent assistant.

[Simplified Chinese](README.zh-CN.md)

WeChat Oracle records WeChat group messages through [WeFlow](https://github.com/hicccc77/WeFlow), stores them in SQLite, optionally OCRs images and transcribes voice messages locally, and lets an LLM-backed bot answer questions inside the group. The bot can also maintain long-term per-group memory in the background.

The project is designed for a personal Windows machine running WeChat PC. Message data, media, memory, and debug logs stay under `data/` unless you explicitly send content to an LLM or vision provider.

## What It Does

- Ingests live WeChat messages from WeFlow SSE into SQLite.
- Imports historical WeFlow JSON exports.
- Stores normalized messages, forwarded-message children, media paths, OCR/ASR transcripts, command runs, agent memory, and audit traces.
- Answers in group chat through wx4py UI automation.
- Supports slash commands such as `/find`, `/sum`, `/recent`, `/ask`, `/explain`, and `/balance`.
- Supports free-form agent chat when directly mentioned, replied to, or probability-triggered.
- Runs a silent `lurk` learning path that updates `group_memory` / `persona_drift` without sending group messages.
- Can use either the native in-process tool loop or an OpenClaw-backed runtime for agent turns.

## Architecture

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

Production usually runs two long-lived processes:

```powershell
uv run wechat-oracle ingest live
uv run wechat-oracle dispatcher
```

`ingest live` starts the SSE subscriber and an embedded mm worker thread. `dispatcher` polls SQLite, processes explicit commands and agent triggers with per-group ordering and cross-group parallelism, and serializes all wx4py sends through one sender thread so only one GUI operation touches WeChat at a time.

## Requirements

- Windows 10/11.
- WeChat PC 4.1.x, Qt version. The wx4py reply path depends on the visible desktop UI.
- WeFlow desktop with HTTP API enabled.
- Python 3.12+.
- [uv](https://docs.astral.sh/uv/).
- An OpenAI-compatible LLM endpoint, or OpenClaw local gateway for `WO_AGENT_BACKEND=openclaw`.

Non-reply workflows such as DB initialization, historical import, status checks, and WeFlow diagnostics can run without wx4py. Sending replies requires WeChat's main window to be visible, not minimized to tray.

## Quick Start

```powershell
git clone https://github.com/<your-account>/WeChat-Oracle.git
cd WeChat-Oracle
uv sync
uv run wechat-oracle init-db
```

Create `.env` in the repository root. Start with the common settings:

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

Then choose one agent backend.

Option A: use an ordinary OpenAI-compatible API directly:

```env
WO_AGENT_BACKEND=native
WO_LLM_API_KEY=<api-key>
WO_LLM_ENDPOINT=https://api.deepseek.com
WO_LLM_MODEL=deepseek-v4-pro
```

Option B: use OpenClaw as the agent runtime:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

Then run:

```powershell
# Terminal 1: live ingest + OCR/ASR worker
uv run wechat-oracle ingest live

# Terminal 2: command/agent dispatcher
uv run wechat-oracle dispatcher
```

In a watched group:

```text
@<bot-name> /help
@<bot-name> Who mentioned stocks today?
@<bot-name> /find since:2026-05 about home renovation
```

If `WO_GROUPS` is empty, live ingest monitors every group chat currently exposed by WeFlow sessions. You can also set `WO_GROUPS` to a comma-separated list of group names, remarks, or wxids.

## Core Commands

General CLI:

```powershell
uv run wechat-oracle init-db
uv run wechat-oracle status
uv run wechat-oracle ingest live
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle dispatcher
uv run wechat-oracle worker mm
```

WeFlow diagnostics:

```powershell
uv run wechat-oracle weflow find <group-name-or-wxid-fragment>
uv run wechat-oracle weflow sessions --groups-only
```

Agent state:

```powershell
uv run wechat-oracle agent show <group_id>
uv run wechat-oracle agent show-runs <group_id> -n 10
uv run wechat-oracle agent lurk <group_id>
uv run wechat-oracle agent wipe <group_id>
uv run wechat-oracle agent wipe <group_id> --persona-only
uv run wechat-oracle agent wipe <group_id> --memory-only -y
```

Health checks:

```powershell
uv run wechat-oracle verify roundtrip
```

OpenClaw runtime helpers:

```powershell
uv run wechat-oracle openclaw ping
uv run wechat-oracle openclaw mcp-test
uv run wechat-oracle openclaw mcp-serve
```

`openclaw mcp-serve` is a stdio MCP server entrypoint intended to be spawned by OpenClaw. `openclaw ping` tests the configured OpenClaw chat-completions gateway.

## Historical Backfill

Backfill imports historical WeFlow exports into the same SQLite archive used by live ingest:

```powershell
uv run wechat-oracle ingest backfill <export.json> --format weflow
```

The command imports one export file at a time. Run it once per exported session file. Re-running the same file is safe: all imported messages go through `ingest/writer.py:write_messages()`, and `UNIQUE(dedupe_key)` skips duplicates.

Supported formats:

| Format | Input |
|---|---|
| `weflow` | WeFlow JSON export with top-level `session` and `messages` fields. |
| `jsonl` | One canonical `Message` JSON object per line, mainly useful for pipeline tests or custom importers. |

For WeFlow exports, the importer:

- Derives `group_id` from `session.wxid`, falling back to the filename stem.
- Derives `group_name` from `session.displayName`, `session.nickname`, or `session.remark`.
- Converts WeFlow `localType` values into normalized message types: text, image, voice, video, sticker, link, quote, forward, and system.
- Preserves quote replies using `reply_to_wx_msg_id` and `quote_text`.
- Parses merged-forward messages into `forwarded_records`, so `/find`, `/sum`, and agent history tools can search inside forwarded bundles.
- Copies referenced local media files from the export folder into `data/media/<group_id>/<kind>/` and stores a `data/`-relative `media_path`.

If a media file referenced by the export is missing, the message is still imported with a missing-media placeholder. OCR/ASR can only run for media files that exist under `data/media`.

Backfilled rows use `source='backfill'`. The dispatcher only wakes on new `source='live'` rows, so importing old messages will not make the bot reply to historical mentions. Backfilled messages are still available to `/find`, `/sum`, `/recent`, agent history search, and manual or automatic `lurk` learning.

## In-Group Commands

The dispatcher accepts explicit slash commands either as `@<bot> /cmd ...` or as a standalone `/cmd ...` group message.

| Command | Purpose |
|---|---|
| `/find [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] <query>` | Semantic search over the current group archive. |
| `/sum [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] [limit:N] [topic]` | Summarize current-group messages. |
| `/recent [N]` | Show recent ingested messages without calling an LLM. |
| `/ask <question>` | Lightweight LLM call with no group-history context. |
| `/explain [text]` | Explain quoted message or supplied text. |
| `/balance` | Query the configured DeepSeek-compatible balance endpoint. |
| `/help [command]` | Show command help. |

Examples:

```text
@Assistant /find from:Alice since:2026-05 about home renovation
@Assistant /sum limit:100 what did people discuss last night?
@Assistant /recent 20
@Assistant /ask What is SQLite WAL?
Reply to an image and send: @Assistant /explain
```

## Agent Behavior

There are three separate runtime paths.

### 1. Collection Path

`ingest live`, `ingest backfill`, and `worker mm` only write normalized data to SQLite. They do not decide whether the bot should speak.

### 2. Interaction Path

The dispatcher classifies each new live message:

| Trigger | Condition | Behavior |
|---|---|---|
| `mention` | Text contains a real `@<WO_BOT_NAME>` mention. | Always wakes the bot. Slash commands are parsed; bare text enters agent chat. |
| `reply` | Message quote-replies to a prior bot message, and `WO_BOT_WXID` is known or auto-discovered. | Always wakes the bot. |
| `probability` | Not mention/reply, passes type gate, random threshold, and cooldown. | Agent decides whether to speak or `stay_silent`. |
| none | No trigger. | No LLM call; message is finalized as no-trigger. |

Direct mention and reply-to-bot triggers run the agent directly. The bot sends a group reply only if the agent returns a non-empty response.

The native agent has two phases:

- Phase A reads recent context, may call read-only tools, and returns either a chat reply or `stay_silent`.
- Phase B sees the Phase A trace and may update `group_memory` or `persona_drift`.

Phase A initial context includes the latest `WO_AGENT_RECENT_CONTEXT_CHAT` messages from the current group. Tools can search older history, expand quote chains, expand forwarded bundles, read OCR/ASR text, read images through a vision model, read voice transcripts, or read group memory.

### 3. Background Learning Path

`agent lurk <group_id>` and optional auto-lurk read a watermarked batch of new messages and update memory without replying.

- First run reads the latest `WO_AGENT_LURK_RECENT_MSGS` messages.
- Later runs continue from `agent_lurk_state.last_msg_id`.
- The lurk agent may call history tools to inspect older messages when the new batch points to prior context.
- It writes only stable, reusable knowledge to `group_memory` or long-term behavior adjustments to `persona_drift`.
- It never calls wx4py and never sends an acknowledgement.

Enable automatic lurk inside dispatcher with:

```env
WO_AGENT_LURK_ENABLED=True
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

Auto-lurk uses a separate single worker, so it does not occupy chat response workers.

## Agent Backends

`WO_AGENT_BACKEND=native` is the default. It runs the tool-calling loop inside this process through your OpenAI-compatible LLM endpoint.

`WO_AGENT_BACKEND=openclaw` delegates chat and lurk turns to an OpenClaw agent through a local OpenAI-compatible gateway. OpenClaw can use the project's MCP server to call WeChat-Oracle tools. This is useful if your production bot should use an existing subscription-backed agent runtime.

Switching between the two modes is controlled by one setting:

```env
# Use the ordinary OpenAI-compatible API directly.
WO_AGENT_BACKEND=native

# Or use OpenClaw as the agent runtime.
WO_AGENT_BACKEND=openclaw
```

Important distinction: OpenClaw here is an agent runtime backend, not a WeChat reply backend. Group replies still go through wx4py or `stdout`.

OpenClaw-related settings:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

## Real WeChat Mentions

`Wx4pyReplier` attempts to create a real WeChat group mention token by opening the group, typing `@`, selecting the requester from WeChat's candidate popup, then pasting the reply body. If the candidate cannot be detected, it falls back to plain text `@name` and logs a warning.

This is intentionally serialized through `_SerialReplier`; multiple worker threads never manipulate the WeChat GUI at the same time.

## Multimedia

The mm worker fills `messages.transcript`:

- Images and stickers: local OCR through `rapidocr-onnxruntime`.
- Voice: local ASR through `faster-whisper`.

Transcript states:

- `NULL`: not processed yet.
- `''`: processed but no text recognized.
- non-empty text: usable OCR/ASR result.

If `WO_VISION_API_KEY` is configured, agent chat and `/explain` / `/ask` can send referenced images to a vision model for direct reading. Without it, native image handling falls back to OCR text and placeholders. In OpenClaw mode, MCP also exposes `load_image`, which returns the original image block for a vision-capable OpenClaw agent to inspect directly; `read_image` still means "return a textual vision-model reading".

## Data Model

For the full ingestion contract, including WeFlow `localType` handling, app-message subtypes, media storage, quote/forward normalization, and LLM-visible rendering, see [docs/message-model.md](docs/message-model.md).

Key tables:

| Table | Purpose |
|---|---|
| `messages` | Normalized WeChat messages. |
| `forwarded_records` | Children inside merged-forward messages. |
| `command_runs` | Dispatcher idempotency and command status. |
| `group_state` | Live/backfill per-group cursors. |
| `persona_drift` | Per-group evolvable behavior supplement. |
| `group_memory` | Per-group freeform long-term memory document. |
| `agent_run_log` | Agent audit traces. |
| `agent_lurk_state` | Lurk cursor, separate from audit logs. |

All write paths for imported messages go through `ingest/writer.py:write_messages()` and rely on `UNIQUE(dedupe_key)` for cross-source deduplication.

`group_memory` and `persona_drift` are replace-on-write. Write tools force the agent to read current state before writing, and they check that state did not change in between. If another concurrent agent run updated memory first, the tool asks the model to re-read and merge.

## Configuration Reference

All runtime settings use the `WO_` prefix and can be set in `.env` or the process environment.

| Variable | Default | Notes |
|---|---:|---|
| `WO_DATA_DIR` | `data` | Data root. |
| `WO_DB_PATH` | `data/wechat-oracle.db` | SQLite database path. |
| `WO_MEDIA_DIR` | `data/media` | Media directory. |
| `WO_GROUPS` | `[]` | Empty means all WeFlow group sessions; comma string or JSON list is accepted. |
| `WO_LOG_LEVEL` | `INFO` | loguru level. |
| `WO_WX4PY_LOG_LEVEL` | `WARNING` | Python logging level for wx4py internals; set to `INFO` only when debugging UI automation. |
| `WO_WEFLOW_BASE_URL` | `http://127.0.0.1:5031` | WeFlow HTTP API root. |
| `WO_WEFLOW_TOKEN` | empty | WeFlow token. |
| `WO_BOT_NAME` | empty | Bot's group nickname. Required by dispatcher. |
| `WO_BOT_WXID` | empty | Optional bot wxid for reply-to-bot trigger. |
| `WO_LLM_PROVIDER` | `openai-compatible` | LLM provider adapter. |
| `WO_LLM_API_KEY` | empty | LLM API key. |
| `WO_LLM_ENDPOINT` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `WO_LLM_MODEL` | `deepseek-v4-pro` | Model name. |
| `WO_LLM_JSON_MODE` | `native` | `native` or `prompt` JSON mode. |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | DB polling interval in seconds. |
| `WO_DISPATCHER_WORKER_THREADS` | `4` | Global message workers. Messages are serialized per group but different groups can run in parallel; wx4py sends remain serialized. |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` candidate cap. |
| `WO_DISPATCHER_CONTEXT_CHAT` | `2500` | Legacy chat context cap, still used by some summary paths. |
| `WO_LLM_MAX_TOKENS` | `5000` | General output cap. |
| `WO_LLM_CHAT_MAX_TOKENS` | empty | Overrides chat cap. |
| `WO_LLM_SUM_MAX_TOKENS` | empty | Overrides summary cap. |
| `WO_LLM_SHORT_MAX_TOKENS` | empty | Overrides `/ask` / `/explain` cap. |
| `WO_LLM_WRITE_MAX_TOKENS` | `10000` | Cap for memory-write tool calls. |
| `WO_VISION_PROVIDER` | `openai-compatible` | Vision adapter. |
| `WO_VISION_API_KEY` | empty | Enables direct image reading. |
| `WO_VISION_ENDPOINT` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Vision endpoint. |
| `WO_VISION_MODEL` | `qwen-vl-plus` | Vision model. |
| `WO_VISION_MAX_IMAGES` | `3` | Per vision request image cap. |
| `WO_VISION_MAX_TOKENS` | `800` | Vision output cap. |
| `WO_AGENT_BASE_PROBABILITY` | `0.25` | Probability trigger threshold; set `0` for explicit triggers only. |
| `WO_AGENT_COOLDOWN_SECONDS` | `30` | Per-group probability cooldown. |
| `WO_AGENT_MAX_STEPS` | `5` | Native Phase A max rounds. |
| `WO_AGENT_REFLECT_MAX_STEPS` | `3` | Native Phase B max rounds. |
| `WO_AGENT_REFLECTION_ENABLED` | `True` | Enables chat Phase B memory reflection. |
| `WO_AGENT_PERSONAS_DIR` | `data/personas` | Persona YAML directory. |
| `WO_AGENT_RECENT_CONTEXT_CHAT` | `100` | Initial recent-message window for agent chat. |
| `WO_AGENT_MEMORY_MAX_CHARS` | `100000` | Hard cap for `group_memory`. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_RUN` | `12` | Native Phase A tool budget. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_STEP` | `4` | Native Phase A per-step tool budget. |
| `WO_AGENT_MAX_IMAGE_READS_PER_RUN` | `2` | Native image-read budget. |
| `WO_AGENT_MAX_VOICE_READS_PER_RUN` | `2` | Native voice-read budget. |
| `WO_AGENT_LURK_ENABLED` | `False` | Enables dispatcher auto-lurk. |
| `WO_AGENT_LURK_INTERVAL_SECONDS` | `1800` | Auto-lurk scan interval. |
| `WO_AGENT_LURK_MIN_NEW_MESSAGES` | `20` | Minimum new messages before auto-lurk. |
| `WO_AGENT_LURK_RECENT_MSGS` | `100` | Max messages processed in one lurk run. |
| `WO_AGENT_LURK_MAX_STEPS` | `4` | Lurk tool-calling rounds. |
| `WO_OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway. |
| `WO_OPENCLAW_TOKEN` | empty | OpenClaw gateway token. |
| `WO_OPENCLAW_AGENT_ID` | `wechat-bot` | OpenClaw agent id. |
| `WO_OPENCLAW_TIMEOUT_SECONDS` | `300` | OpenClaw gateway request timeout. |
| `WO_AGENT_BACKEND` | `native` | `native` or `openclaw`. |
| `WO_REPLY` | `True` | Send replies back to WeChat. |
| `WO_REPLY_BACKEND` | `wx4py` | `wx4py` or `stdout`. |

`WO_WHISPER_MODEL` is read directly by the mm worker and defaults to `small`; accepted values depend on faster-whisper, commonly `tiny`, `base`, `small`, `medium`, and `large-v3`.

## Logs and Debugging

- `data/dispatcher.log`: compact human-readable command and agent traces.
- `data/llm_debug.log`: LLM prompts, raw replies, parsed results, and full traces.
- `data/events.jsonl`: lightweight machine-readable lifecycle events for ingest batches, trigger decisions, command/agent runs, native tool calls, memory writes, and reply attempts. Use this to answer operational questions such as "why did this message trigger?" or "where did the latency go?" without reading full prompts.
- `data/*.process.log`: rotating loguru process logs for long-running processes such as `live` and `dispatcher`.
- `data/openclaw.log` / `data/mcp.log`: JSONL audit logs for OpenClaw gateway calls and MCP tool invocations.
- `wx4py_send_audit.jsonl`: wx4py send audit records when wx4py emits them.

Useful checks:

```powershell
uv run wechat-oracle status
uv run wechat-oracle verify roundtrip
uv run wechat-oracle agent show-runs <group_id> -n 20
```

`verify roundtrip` confirms whether WeFlow echoes the bot's own wx4py-sent messages back into `messages`. That echo is required for bot wxid auto-discovery and reply-to-bot triggers.

## Known Limits

- wx4py depends on a visible Windows desktop and WeChat Qt UI. It is not an official WeChat API.
- Group reply delivery is implemented only through `wx4py` or local `stdout`.
- OpenClaw is supported as an agent runtime backend, not as a group-message reply backend.
- If WeFlow does not expose historical media files, old images/voices imported by backfill cannot be OCRed/transcribed until you provide media.
- Video content is stored as metadata/path only; video understanding is not implemented.
- Deduplication is best-effort when WeFlow lacks `wx_msg_id`.

## Development

This repository currently has no pytest, ruff, mypy, or CI test suite. Use the local checks below before committing:

```powershell
uv run python -m compileall src\wechat_oracle
uv run wechat-oracle agent --help
uv run wechat-oracle status
uv run python .Codex\hooks\check_doc_sync.py
git diff --check
```

Enable the git hook once per clone:

```powershell
git config core.hooksPath .githooks
```

When changing schema, CLI commands, config fields, or dispatcher command syntax, update this README in the same commit.

## License

MIT. See [LICENSE](LICENSE).
