# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 架构一句话

两个常驻进程（`ingest live` / `dispatcher`）+ 一次性 `ingest backfill`，共享一份 WAL 模式的 SQLite (`data/wechat-oracle.db`)。**WeFlow 的 HTTP API 是上游唯一真相源**——live 走 SSE 推流，backfill 导 WeFlow JSON 导出，没有路径直连微信原始 DB。`ingest live` 进程内开 daemon 线程跑 mm worker（OCR/ASR），共享 SQLite 写入靠 WAL 隔离。dispatcher 轮询 DB，命中 `@<bot>` 文本后走 agent loop（多轮 tool-calling），用 wx4py（Windows UI 自动化）把回复打回群。

数据形态：`messages`（主表）+ `forwarded_records`（合并转发子项，`parent_msg_id` 反指）+ `command_runs`（dispatcher 幂等记录，`msg_id` 主键）。所有写入路径必须走 `ingest/writer.py:write_messages`，靠 `UNIQUE(dedupe_key)` 跨源去重。详细字段语义看 `schema.sql`（DDL 行级注释是主源）+ `models.py` docstring。

## 常用命令

依赖管理 / 装环境（项目用 `uv`，Python ≥ 3.12）：

```bash
uv sync                                # 装依赖
uv run wechat-oracle init-db           # 建表（幂等，每次跑前先来一遍也无所谓）
uv run wechat-oracle status            # 查总条数 / 按 status / 按群分布——快速 health check
```

长跑 / 一次性进程（生产路径上 `live` + `dispatcher` 同时常驻）：

```bash
uv run wechat-oracle ingest live                                 # SSE 实时抓 → DB；同进程内跑 mm OCR/ASR daemon 线程
uv run wechat-oracle dispatcher                                  # DB 轮询 → agent loop / LLM → wx4py 回群
uv run wechat-oracle ingest backfill <path.json> --format weflow # 一次性导入 WeFlow JSON 导出
uv run wechat-oracle worker mm                                   # 单独跑 mm（一般不用，ingest live 已包；调试或追识别 backfill 行时用）
```

诊断 `WO_GROUPS` 解析（群名找不到对应 wxid 时用）：

```bash
uv run wechat-oracle weflow find <群名 / 备注 / wxid 片段>   # 同时查 contacts + sessions
uv run wechat-oracle weflow sessions --groups-only          # 列出所有 @chatroom 会话
```

封装好的 wrapper（POSIX）：`scripts/import.sh <export.json>` / `scripts/track.sh`；Windows 对应 `.bat` 同名文件。

**测试 / lint：本项目当前没有 pytest、ruff、mypy 或任何 CI 配置**——`pyproject.toml` 里没有 dev-dependencies 段，仓库里也没有 `tests/`。改代码要自验，靠跑上面那几条 + 看 `data/dispatcher.log` / `data/llm_debug.log`。要加单测的话先和用户对一下要不要顺便引一套 pytest。

## 平台前提

**生产环境是 Windows + 中文 WeChat 4.1.x（Qt 版）+ WeFlow 桌面端**——wx4py 走 UI 自动化只在 Windows 工作，dispatcher 的「发回群」分支在 macOS/Linux 上跑不起来。但**导入 / 查询 / DB 操作（init-db / backfill / status / weflow find）跨平台**，本仓库的 dev worktree 多半就在 macOS 上。改 dispatcher 时如果 Windows 不在手边，至少要确保 `parse_command` / SQL 构造 / LLM 调用这些纯逻辑路径能在本地手动 invoke 验证。

## 文档导航（新会话先读这段）

- **本文件**：项目特定的契约 / 用户偏好 / 踩过的坑（Lessons Learned）。冲突时**项目级 > 全局基线**。
- **`README.md`**：对外用户文档。改命令 / 配置 / schema / 入口都要回头同步。「数据流细节」段对理解三个进程怎么协作最有帮助。
- **代码内 docstring**：模块顶端有 5–10 行职责说明，`dispatcher.py` / `live.py` / `forwarded.py` 写得最详细。
- **`schema.sql`**：DDL 行级注释是字段语义的**主源**，发现描述模糊就在这儿改。
- **`.claude/hooks/check_doc_sync.py`**：自动 backstop，跨文件漂移会反向提醒。

按场景找入口：

| 我要改… | 先读 | 同步目标（见「易漂移点速查」） |
|---|---|---|
| slash 命令的语法 / 加新命令 | `dispatcher.py` 顶端 docstring + 「命令体系维护契约」 | F4 |
| 入库字段 / 新加列 | `schema.sql` + `models.py` + 「管道字段对齐」 | F1 / F2 |
| 配置项 | `config.py` `Settings` | F3 |
| CLI 子命令 | `cli.py` | F5 |
| 新 importer / 数据源 | `ingest/writer.py` 的 `write_messages` 入口 | F1 / F11 / 管道字段对齐 |
| WeFlow API 字段语义 | `live.py` 顶端 + `forwarded.py` 顶端 docstring | F11 / Lessons Learned |
| dispatcher 查询逻辑 | `dispatcher.py` 的 `fetch_candidates` | F2（要 UNION forwarded_records） |
| Agent loop / 加 tool / 改记忆表 | `agent/runtime.py` + `agent/tools_read.py` + `agent/tools_write.py` + `agent/persona.py` + `agent/orchestrator.py`（dispatcher↔agent 桥） | F17（必看：group_id 隔离 / 写语义 / msg_id 整数约定） |
| 视觉模型调用 | `agent/tools_read.py:ReadImageTool` 或 `dispatcher.py:_run_vision_on_quoted_image` | F16 |

## 用户偏好

- **先想后动**：动手前先把意图、权衡、影响范围想清楚再写代码。
- **简单但不简陋**：选最直接的实现，能用一个函数说清楚的事不要拆三层。
- **不堆错误处理 / 冗余函数**：除非确有必要或用户明说，不要预防性地加 try/except、参数校验、辅助包装。健壮性靠数据契约和显式失败，不靠到处兜底。
- **改动有取舍时先问用户**：涉及策略选择（路径布局、复制 vs 引用、是否删数据等），列出 trade-offs 让用户拍板，不擅自决定。
- **destructive 操作必须确认**：删 DB 行、清目录、`git reset --hard` 之类，先讲清后果再执行。

## 项目特性约定

- **数据本地优先**：`data/` 是项目自有归档，导入时把媒体复制进来（`data/media/<group_id>/<kind>/`），不留对外部路径的依赖。
- **跨源去重**：所有写入路径走 `write_messages()` → `UNIQUE(dedupe_key)`，新增 importer 时复用、不要绕过。
- **`source` 字段记录管道来源**（`live` / `backfill`），不要用它表达消息状态——状态走 `status` 列。
- **WeFlow 是唯一真相源**：实时抓和历史回灌都过 WeFlow，不直接读微信原始 DB。
- **dispatcher 冷启动不回放历史**：`_skip_backlog` 在启动时把所有未处理的 `@bot` 历史消息标 `(startup-skip)`，避免冷启动 / 大批量回灌后向群灌一通陈年答复。改这个行为要同步 README 数据流段。

## 易漂移点速查

同一个事实被多处编码就会漂移。下表列出本仓库所有冗余存储位置；改任何一处，**必须**主动看其它行的状态。`.claude/hooks/check_doc_sync.py` 覆盖 F1–F5，其它行靠人 + 本契约保障。

| ID | 事实 | 存储位置 | 自动校验 | 注意点 |
|---|---|---|---|---|
| **F1** | `messages` 表 schema | `schema.sql` + `models.py:Message` + `ingest/writer.py:INSERT_SQL/_row` + `README.md` 数据库段 | hook | 加列要四处全改 |
| **F2** | `forwarded_records` schema | `schema.sql` + `models.py:ForwardedItem` + `ingest/writer.py:INSERT_FWD_SQL` + `ingest/forwarded.py` 解析 + `README.md` | hook | 加 datatype 占位时同步 `_PLACEHOLDER` |
| **F3** | `WO_*` 配置项 | `config.py:Settings` + `README.md` 配置参考表 + `README.md` 快速上手 .env 示例 | hook | 删字段记得 .env 例子也清掉 |
| **F4** | slash 命令集 | `dispatcher.py` `Command` 子类 + `README.md` 命令详解 | hook | `Command.help()` 自动从类 attr 生成；改 attr 时同步 README |
| **F5** | CLI 子命令 | `cli.py` typer 命令 + `README.md` 快速上手 + 三进程表 | hook | 加进程要同步 README 三进程表 |
| F6 | localType→MsgType 映射 | `ingest/backfill.py:_WEFLOW_LOCAL_TYPE_MAP` + `ingest/forwarded.py` 常量 | — | live.py 通过 import 复用，单源 |
| F7 | `MsgType` enum 值 | `models.py:MsgType` + `schema.sql` `type` 列注释 + `dispatcher.py:fetch_candidates` SQL CASE 占位映射（image/voice/video/sticker） + `README.md` schema 段 | — | 加新 type 要同步：enum、CHECK、SQL CASE 占位（如新 type 经常无 content_text）、README。**fetch_candidates 不再按 type 过滤**——所有类型都进候选池 |
| F8 | 状态枚举（`Status` / `source` / `command_runs.status`） | `models.py` enum + `schema.sql` CHECK + 字面量散布 + `README.md` | SQLite CHECK 兜底 | CHECK 失败时是 IntegrityError，不会静默 |
| F9 | `dedupe_key` 拼装规则 | `models.py:compute_dedupe_key` + `schema.sql` 注释 + `README.md` 跨源去重段 | — | 改算法 = 历史数据 dedupe 失效，需要清库 |
| F10 | 显示名优先级 (groupNickname > remark > nickname > displayName) | `ingest/live.py:_pick_member_display` + `schema.sql` `sender_display` 注释 | — | backfill 走 `senderDisplayName` 单字段，没有这个优先级链 |
| F11 | WeFlow `(appmsg.type<<32)\|49` localType 编码 + 各 subtype 处理 | `ingest/forwarded.py`（常量 + 模块 docstring 列出全部 subtype） + `ingest/live.py:_api_msg_to_normalized` 分支 + 本文件 Lessons Learned + `README.md` appmsg 段 | — | 加新 appmsg subtype 要同步：常量、live 分支、forwarded.py docstring 表、README |
| F12 | 媒体子目录布局 (`images`/`voices`/`videos`/`stickers`) | `ingest/media_store.py:MEDIA_SUBDIR` + `ingest/live.py` + `ingest/backfill.py` + `README.md` 媒体处理段 | — | live/backfill 都必须把本地媒体复制进 `data/media/<group_id>/<kind>/` 并在 DB 存相对 `data_dir` 的路径；改字典等于把已有 `data/media/<旧>/` 全部弃用 |
| F13 | dispatcher `_skip_backlog` 启动行为 | `dispatcher.py:_skip_backlog` + `README.md` 数据流细节段 | — | 改默认行为（比如改成处理积压）必须同步 README + 给个 flag |
| F15 | `messages.transcript` 语义（OCR/ASR 出口）+ LLM 可见标签 | `schema.sql` 注释 + `models.py:Message.transcript` docstring + `worker/mm.py` 状态机注释 + `dispatcher.py:fetch_candidates` SQL CASE 拼接逻辑 + 两条 system prompt（`_CHAT_SYSTEM_PROMPT` / `_SYSTEM_PROMPT`）里描述的标签形状 + `README.md` 多媒体识别段 | hook (schema.sql 改触发) | 三态：`NULL` 待处理 / `''` 已处理无文字（不重试） / `'<text>'` 成功。**LLM 可见标签**：有 transcript 时是 `[图片·OCR] xxx`/`[语音·ASR] xxx`（中点 `·` 区分 "已识别" vs "事件占位"），无 transcript 仅 `[图片]`/`[语音]`。改 SQL CASE 的标签形状 → 必须同时改两条 prompt 的格式说明，否则 LLM 看不懂会答"群里没出现过相关讨论" |
| F14 | 本 hook 的 marker 列表 | `.claude/hooks/check_doc_sync.py:_*_MARKERS` + 本文件「命令体系维护契约」判定标准段 | — | 改 marker 时把 prose 描述也改了，否则 hook 和契约说的不是一回事 |
| F15 | `WO_REPLY_BACKEND` 取值集 (`wx4py` / `stdout`) | `replier.py:build_replier` if-chain + `config.py:reply_backend` 注释 + `README.md` 配置参考表 | hook (config.py 改触发) | 加新 backend 时把这三处都同步；新建一个 `XxxReplier` 类实现 `Replier` 协议即可，不动 dispatcher。**Tencent iLink Bot 不在列表里**——实测不可群发，见 README「实验记录」 |
| F16 | 视觉模型调用点（agent `read_image` 工具 + /explain & /ask 单轮直读） | `llm.py:VisionLLM/OpenAICompatVisionLLM/build_vision_client` + `agent/tools_read.py:ReadImageTool` + `agent/media_paths.py:resolve_media_path_for_msg/resolve_quoted_image_path` + `dispatcher.py:_run_vision_on_quoted_image/_resolve_quoted_image_path/ExplainCommand._execute_vision/AskCommand._execute_vision` + `config.py:vision_*` + `README.md`「视觉模型集成」段 | hook (config.py / dispatcher.py 改触发) | **两类调用路径**：(1) agent loop 自由问答里模型自己调 `read_image(msg_id, prompt?)`——单张直读，把视觉模型描述放进 trace 继续推理；(2) `/explain` & `/ask` 引用图片走单轮直读，用户已指定哪张图。两类都用同一个 `VisionLLM`。**单轮直读（命令路径）统一走 `_run_vision_on_quoted_image`**——加新 vision 命令时复用这个函数，差异只在 system prompt + user prompt + label 上。**agent 路径走 `ReadImageTool.call` 走 `media_paths.resolve_media_path_for_msg`**——同样路径解析逻辑，但调用入口在工具签名里。`WO_VISION_API_KEY` 空 = 整个 vision 关闭：`ReadImageTool` 抛 `ToolError("vision not configured ...")`，`/explain` & `/ask` 引用图退到文本路径（看到 `[图片]` 占位）。`WO_VISION_MAX_IMAGES=3` 在 vision client 层做最终兜底（agent `read_image` 一次只送 1 张但可被多次调用）。**注意：旧的 `<NEED_IMAGES>` sentinel 二轮协议已删（commit 7）**——`chat_assistant` / `_NEED_IMAGES_RE` / `_extract_need_images` / `_CHAT_SYSTEM_PROMPT_VISION_TAIL` 全部不存在，看到这些名字别去 grep 找了 |
| F17 | Agent loop 体系（chat 路径 + 三张表 + 工具集 + persona 装配 + dispatcher 集成 + CLI） | DDL: `schema.sql:persona_drift/group_memory/agent_run_log`（+ 历史遗留 member_notes/group_notes，已不再写）+ 配置: `config.py:agent_*`（base_probability / cooldown_seconds / max_steps / reflect_max_steps / reflection_enabled / personas_dir / recent_context_chat / **memory_max_chars**） + 运行时: `agent/runtime.py:run_phase_a/run_phase_b/run_lurk_reflection/_run_simple_tool_loop/run_agent`（Phase B + lurk 共用 `_run_simple_tool_loop`，加新静默循环优先扩这个） + 工具基类: `agent/tools.py:Tool/GroupScopedTools/ToolError/StaySilentTool` + Phase A: `agent/tools_read.py:register_phase_a_tools`（recall/view_quoted/expand_forward/read_image/read_voice/**read_group_memory** + stay_silent） + Phase B: `agent/tools_write.py:register_phase_b_tools/phase_b_system_prompt/trace_touched_tables`（**update_persona_drift / update_group_memory** + read_* 子集） + 持久化 DAO: `agent/memory.py:get_persona_drift/upsert_persona_drift/get_group_memory/upsert_group_memory/link_last_run_id/insert_run_log/list_recent_runs` + 迁移: `db.py:_migrate`（自动加缺失列 `last_run_id`） + 路径解析: `agent/media_paths.py` + 人格装配: `agent/persona.py:load_persona_yaml/build_phase_a_system/build_phase_b_system/assemble_system_prompts/_join_nonempty` + LLM tool-calling: `llm.py:OpenAICompatLLM.complete_with_tools/ToolingLLM/ToolCall/ToolingResponse` + dispatcher↔agent 桥: `agent/orchestrator.py:chat_via_agent/chat_via_lurk/lurk_due_groups`（trace 渲染 / recent 拉取 / lurk 游标 SQL 都在这里；dispatcher.py 不再直接 import agent.memory / agent.runtime）+ 触发与调度: `dispatcher.py:_classify_trigger/_process_agent_only/ChatCommand.execute/_resolve_bot_wxid/_GlobalScheduler/_LurkScheduler` + 共享日志: `log_utils.py:append_log/dump_llm_call`（dispatcher.log + llm_debug.log 单源，dispatcher 和 orchestrator 都用） + CLI: `cli.py:agent_app`（show / wipe / show-runs） + 数据: `data/personas/<group_id>.yaml`（用户手编） + 模板: `agent/example_persona.yaml` + 文档: `README.md`「自由问答兜底」段 + 「数据库 schema」agent 三表段 + 配置参考 `WO_AGENT_*` 行 | hook (schema.sql / config.py / cli.py 改触发) | **关键约束**：`group_id` 不暴露给 LLM 当 tool 参数——所有 SQL 在工具实现里 hardcode `WHERE group_id=?`，参数从 `GroupScopedTools.__init__` 锁定。加新工具必检查这条 invariant。**写语义**：`persona_drift` / `group_memory` 都是替换语义（agent 先读再决定全文整段写回）；`agent_run_log` 始终追加（审计）。`group_memory` 是**单一 freeform 文档**（成员/事件/群文化全放一起，agent 自己组织内部结构），上限 `WO_AGENT_MEMORY_MAX_CHARS=100000`，update 时超过抛 ToolError 强制压缩。**raw↔summary 链接**：`persona_drift.last_run_id` / `group_memory.last_run_id` 反指 `agent_run_log`，`orchestrator.chat_via_agent` / `chat_via_lurk` 在 `insert_run_log` 后用 `trace_touched_tables` + `link_last_run_id` 一起写——任何记忆内容都能 trace 回写它的那次 run。**改语义 = 数据契约破坏**。**msg_id 格式**：agent 工具一律收 `int`，不是 `/find` 的 `m:N` / `f:N` 字符串——别把两套混了。**触发层**：`dispatcher._classify_trigger` 给每条 live 消息打三类标签——`mention`（含 `@<bot_name>`）/ `reply`（quote-reply 指向 bot 自己的消息，需要 bot_wxid）/ `probability`（`WO_AGENT_BASE_PROBABILITY > 0` 且过了 `WO_AGENT_COOLDOWN_SECONDS`），`None` 则跳过不烧 LLM。`mention` 走 `parse_command` 流程（slash 命令仍 work），其余直接走 agent。bot_wxid 来源：`WO_BOT_WXID` 显式 > `_resolve_bot_wxid` 自动发现（从 `messages` 表里 `sender_display=WO_BOT_NAME` 的回流消息拿）；`uv run wechat-oracle verify roundtrip` 诊断回流。**用户控制阀门**：`uv run wechat-oracle agent show / show-runs / wipe` 可以读笔记 / 看演化历史 / 清空 — 用户对 bot 演化方向的最终保险。**人格默认极简**（commit history: 删了刻意的 voice / 回答风格规则）：默认只剩 identity + ops_rules（4 行），其它通过 yaml 主动填或通过 `persona_drift` 演化得到。 |**Phase B 运作方式**：`reflection_enabled=False` 时直接跳过；启用时模型可以选择 0 写入（输出空文本结束）。新加 write 工具时记得在 `register_phase_b_tools` 注册；Phase B 也要保留对应的 read 工具方便先读再写 |
| F18 | OpenClaw backend stack | `config.py:agent_backend/openclaw_*` + `llm.py:OpenClawChatCompletions/OpenClawCompletionLLM` + `agent/backend.py` + `agent/backends/openclaw.py` + `agent/orchestrator.py:chat_via_lurk` + `mcp_server.py` + `cli.py:openclaw_app` + `scripts/register_mcp.ps1` + `scripts/register_mcp.sh` + `examples/openclaw/*` + `README.md` OpenClaw setup | — | OpenClaw runs on Windows from the same checkout and normal project `.venv`. MCP tools take explicit `group_id`; keep group isolation and read-before-write memory semantics aligned with native tools. In `WO_AGENT_BACKEND=openclaw`, chat triggers, slash-command text/JSON completions, and lurk reflection all use the OpenClaw gateway. **`read_image` divergence**: the MCP-side `read_image(group_id, msg_id)` returns the raw image as a FastMCP `Image` content block (not text), so the wechat-bot agent's own vision sees pixels directly; `WO_VISION_*` is unused in openclaw mode. Native `agent/tools_read.py:ReadImageTool` is unchanged and still hits the configured vision API. |

**新事实进表的判定**：如果你引入了一个事实它**注定要在两个以上文件里出现**（即使本仓库现在只放在了一处），就把它登记到本表，并尽量用单源 + import 替代多处复制。

**两道闸**：
- `.claude/hooks/check_doc_sync.py`（PostToolUse）——Claude 编辑文件**当下**就反向提醒，看的是 working tree
- `.githooks/pre-commit`（git）——`git commit` 时**最后一道**关，看的是 staged-only 视图，挡掉手工编辑 / rebase / 不同 agent 漏掉的情况；额外还跑 schema parse / Python 语法 / API key 扫描 / `.env` 防泄露

PostToolUse 是 prompt 时机的提醒（可被忽略 / 可继续编辑修复），pre-commit 是**真硬拦**（exit 1 直接 abort commit）。两个共用同一份 `RULES` 表。

## 命令体系维护契约

`dispatcher.py` 里的 `Command` 子类（`FindCommand`、`ChatCommand`、`HelpCommand` 等）改动时，**这三处必须保持一致，否则用户拿到的帮助就是骗人的**：

1. **类属性 `name` / `usage` / `description` / `examples`** —— `Command.help()` 是 classmethod，从这些 attr 自动生成 `/help` 输出。**不用手写 help 函数体**，改 attr 就够；但反过来：改了 `parse()` 的语法（加 marker、改默认值、删 flag）就**必须**回头同步 attr，不然 `/help` 会撒谎。
2. **`README.md` 的「命令详解」段** —— 这是给人读的对外文档；CI 不会校验它和代码一致性。**任何**命令的新增 / 删除 / 重命名 / 语法改动 / 默认值改动，都必须同步这段。
3. **`parse_command` 的入口路由**（`@<bot> /xxx` 解析、`@<bot> <free text>` 兜底分支）—— 改了入口规则也算命令体系改动，README 「命令详解」+ 「错误反馈」段都要看。

**判定标准（PR / commit 自检）**：本次 diff 里 `dispatcher.py` 命中以下任一行 ≥ 1 处：
- `name = ...` / `usage = ...` / `description = ...` / `examples = [...]`（命令类常量）
- `class ...Command(Command):` / `@register`（命令本身的增删）
- `def parse(`（解析逻辑变了 → 检查 usage/examples 是否还对得上）
- `parse_command(` 函数体（路由逻辑）

→ 那 `README.md` 的「命令详解」**也必须同 PR 改**。

`.claude/hooks/check_doc_sync.py`（覆盖 F1–F5）是自动 backstop：dispatcher.py 上述行被改但 README.md 这次没动时，hook 会反过来提醒下一次工具调用。**hook 不是合规的替代**——以本契约为准，hook 失灵不是免责理由。

## 管道字段对齐（Lessons Learned）

`messages` 表的 schema 是**结构契约**，不是**语义契约**——同一个字段在不同 importer 里可能被填得完全不同；下游（dispatcher、dedupe、查询）按某种语义去用，就会出 bug。**新增 importer 或新增字段时，必须把字段语义在所有 importer 之间对齐**，并在 PR 里显式说明。

已踩过的坑：

- **`sender_display`**：backfill 从 WeFlow JSON 的 `senderDisplayName` 拿到真名；live 早期用 `senderUsername`（wxid）兜底，导致 dispatcher 按显示名查全部落空。修法：live 启动时 `/api/v1/group-members` 拉 roster 建 wxid→display 映射。**教训**：「API 没返回某字段」不是「字段缺失」，必须查清楚是不是别的 endpoint 提供，否则下游用起来会静默错。
- **`media_path`**：早期 backfill 存项目内相对路径（`media/<group>/...`），live 存 WeFlow 给的 Windows 绝对路径（`mediaLocalPath`），导致 MCP `read_image` 依赖外部 WeFlow cache，也让没有 `wx_msg_id` 的媒体消息 fallback `dedupe_key` 跨管道不一致。修法：`ingest/media_store.py` 统一 live/backfill 行为，入库前把本地媒体复制进 `data/media/<group_id>/<kind>/`，DB 只存相对 `data_dir` 的路径；历史绝对路径用 `scripts/normalize_media_paths.py` 迁移。
- **`localType` 高位编码**：WeFlow 把 `<appmsg>.<type>` 偷塞进 localType 高 32 位（`localType = (appmsg.type << 32) | 49`）。早期 `_WEFLOW_LOCAL_TYPE_MAP` 只查整个 localType，对所有 app 消息都返回 None → **整条消息被 importer 丢弃**（包括合并转发、引用、链接卡片）。修法：所有查表前先 `base_local_type(lt)` 抹掉高位；需要细分的（如 `appmsg.type=19` 合并转发）用 `appmsg_subtype(lt)` 取出来另判。**教训**：第三方 API 返回的「整数字段」可能是组合编码，光按枚举查表会静默吃掉一类消息——加新格式前，先把分布拉出来看一眼。
- **`forwarded_records.t` 不是入库时间**：合并转发子项的 `t` 来自源群里原消息的 `<srcMsgCreateTime>`，可能比包它进来的 wrapper 早数年。dispatcher 的 `since:` 过滤要按子项自己的 `t` 算（不是 wrapper 的 `t`），否则 `since:2024` 这种查询会漏掉「2024 的消息被 2026 转发进来」的情况。
- **合并转发的 `<dataitem>` schema 跨版本变了**：早期格式时间戳在顶层 `<srcMsgCreateTime>`，新格式（≈ 2026-05 后看到）搬进了 `<dataitemsource><createtime>`，顶层只剩 `<sourcetime>` 字符串。**parser 早期只查 `<srcMsgCreateTime>`，新格式 dataitem 直接被静默丢弃 → 整条转发包 0 children**。修法：`_extract_dataitem_timestamp` 三级 fallback（顶层数字 → `dataitemsource/createtime` → `sourcetime` 字符串解析）。**教训**：第三方应用解析微信的 schema 不是定死的——同一个 appmsg.type 在不同微信客户端版本 / 不同 WeFlow 版本下字段可以位置漂移，**任何需要"必填"判断的字段都要兜 fallback 或显式 fail-loud**，否则一次客户端升级就能让一类消息批量沉默丢失。
- **`localType=49` 是个垃圾桶**：所有 `<appmsg>` 包裹的消息都走这条道——链接卡片 / 文件 / 视频号 / 红包 / 转账 / 引用回复 / 合并转发——区分靠 `appmsg.type`（在 WeFlow 的 `localType` 高 32 位）。**早期把 49 一律映射成 `MsgType.LINK` → 引用回复（appmsg.type=57，用户实际打的字在 `<title>` 里）全被 dispatcher 的 `WHERE type='text'` 过滤掉了**——bot 看不到「张三引用某人的话回复了什么」。修法：live `_api_msg_to_normalized` 按 `appmsg_subtype` 分支：57→QUOTE、19→FORWARD、其它→LINK；dispatcher 把 `type IN ('text', 'quote')` 都纳入候选；非文本子类（链接/文件/视频号）由 `format_appmsg_content` 提炼成 `[标签] 标题\nURL` 形式塞进 `content_text`。**教训**：第三方 API 暴露的"消息类型"字段不一定是叶子分类——上面常常还有一层 schema 内编码（`<appmsg>.<type>`），把这层吞掉就等于把一大类消息的语义打平了。新接入数据源时先把所有"高层 type=X" 的子类型分布拉一遍。
- **quote_text 两源填法不一致 → 出口合并**：backfill 的 `quotedContent` 是 WeFlow JSON 文件预拼好的，`content_text` 直接长成 `<reply>[引用 <orig>:<quoted>]`，`quote_text` 列只是冗余备份；live 的 `content_text` 只有用户回复（"2+2"），`quote_text` 单独存。早期 dispatcher.fetch_candidates 只 SELECT `content_text` → live 的引用回复给 LLM 看就只剩 "2+2"，没上下文，模型每次都问"什么意思"。修法：在 fetch_candidates 的 SQL 出口处用 CASE，遇到 quote 行且 `content_text` 不含 `[引用` 标记时拼接 `[引用 <LEFT JOIN orig.sender>：<quote_text>]`。**教训**：数据契约对齐的最后一道关在**消费端**，不只是入库端——同一字段在不同 source 的填充格式不一样时，下游 query 必须做归一，否则下游业务（这里是 LLM 喂 prompt）会按某一种格式假设走，另一种就静默坏掉。每加新 importer/查询都要问：「这个字段下游用的时候期待什么形状？」
- **/find 和 chat 视野其实只该差一项**：第一版 fetch_candidates 用 `WHERE type IN ('text','quote')` → chat 看不到链接 / 图片 / 系统 / 转账，"刚才那个链接讲什么"答不了。第二版区分 `for_chat` 模式让 chat 全量、`/find` 仍严格——理由是怕"链接标题污染搜索"。但实际：LLM 自己能区分 `[图片]` 占位 vs 用户文字，"`/find` 关于股票的讨论" 命中"`[链接] 美股周报`" **本来就该算相关结果**（分享文章也是参与）。最后**两模式合并到同一份 SQL，只剩"是否保留 /xxx 命令消息" 这一项差异**——/find 排除（其他人的命令不是当前查询的信号），chat 保留（是对话流程）。**教训**：先别假设"严格更精确"——有现成 LLM 判断力的场景下，过度过滤往往是把信号当噪声扔了。两个貌似不同的查询入口出现"几乎一样的代码 + 一两处岔分支"时，先怀疑是不是抽象划错了，能合则合。

落实办法：
- 新 importer / 新字段定义时，**先在 `models.py` 的 docstring 里写清字段语义**，再写 importer。
- 下游模块（dispatcher 之类）若按某字段查询，**在 PR 里点名所有 importer 的填充策略**，确认对齐。
- 单元测试里至少为每个 importer 跑一条 fixture，断言关键字段语义（不是结构）相同。
