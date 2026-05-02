# WeChat-Oracle

> 本地化的微信群聊归档 + 可问答助手。把群历史攒成 SQLite，用 LLM 在群里直接 @ 它问问题。

A local-first WeChat group-chat archiver with an LLM-backed in-group Q&A assistant. All chat data stays on your machine; the bot answers questions like _「谁今天提到了股票？」_ or _「张三上次说数学和物理哪个更难？」_ by searching the local archive.

---

## 它能做什么

- **实时抓取**：订阅 [WeFlow](https://github.com/hicccc77/WeFlow) 的 SSE 推送，新消息毫秒级落库
- **历史回灌**：导入 WeFlow 的 JSON 导出，把媒体复制进项目自有的 `data/media/`，一次导入永久脱钩源目录
- **群内问答机器人**：在群里 @ 小号，触发以下三类操作
  - `/find <描述>` — 语义检索群历史（DeepSeek 精筛 + 关键词兜底）
  - `/help` — 查命令
  - **自由问答**（无 `/` 命令）：把最近 5000 条群消息当上下文，让 LLM 直接答
- **本地优先**：消息、媒体、调试日志全在 `data/`，无云端依赖（除了 LLM API）

## 整体架构

```
WeChat 客户端 ──► WeFlow（解密本地 DB 提供 HTTP API）
                       │
        ┌──────────────┼──────────────────────┐
        │              │                      │
   /api/v1/messages   SSE push           /api/v1/messages
   （历史导入）        （实时）            （查询时拉群成员）
        │              │
        └─► backfill ──┴─► live ──► SQLite (data/wechat-oracle.db)
                                        │
                                        ▼
                                  dispatcher ──► DeepSeek LLM
                                        │              │
                                        ▼              ▼
                                  data/dispatcher.log  data/llm_debug.log
                                        │
                                        ▼
                                  wx4py 把回复打回群里
```

三个进程相互独立，跑在 WAL 模式的同一份 SQLite 上：

| 进程 | 职责 | 入口 |
|---|---|---|
| `ingest live` | SSE 订阅 + 写库 | `uv run wechat-oracle ingest live` |
| `dispatcher` | 检测命令 → LLM → 群里回复 | `uv run wechat-oracle dispatcher` |
| `ingest backfill` | 一次性导入历史 | `uv run wechat-oracle ingest backfill <file>` |

---

## 前置条件

- **Windows 10/11**（wx4py 走的 UI 自动化只支持 Windows）
- **微信 PC 4.1.x（Qt 版）** — wx4py 实测 4.1.7.59 / 4.1.8.29，4.1.9.30 可用；中文 UI 推荐
- **WeFlow 桌面端** — 装好并能解密你的 WeChat 数据；启动 HTTP API 服务
- **Python 3.12+**，**[uv](https://docs.astral.sh/uv/)** 管理依赖
- **DeepSeek API Key**（dispatcher 走 LLM；OpenAI 兼容接口）

> ⚠️ wx4py 通过 Windows UI Automation 模拟键鼠操作微信，理论上不被官方支持。封号风险存在但远低于注入式工具（wcferry 等）。仅在你愿意承担风险的小号上使用。本项目作者对账号问题不负责。

---

## 快速上手

### 1. 装

```powershell
git clone https://github.com/<你的-fork>/WeChat-Oracle.git
cd WeChat-Oracle
uv sync
```

### 2. 配 `.env`

项目根目录创建 `.env`：

```env
# WeFlow（必填）
WO_WEFLOW_TOKEN=<WeFlow 设置 → API 服务里复制的 token>
WO_GROUPS=<你要监听的群名>      # 群名 / 备注 / wxid，逗号分隔多个

# Dispatcher（用群内问答时必填）
WO_BOT_NAME=<小号在群里的群昵称>
WO_DEEPSEEK_API_KEY=sk-...
# WO_DEEPSEEK_MODEL=deepseek-v4-pro    # 默认 deepseek-v4-pro，可换 flash 省钱
# WO_REPLY=0                           # 默认 1 = 自动回到群里；0 = 只本地输出
```

### 3. 建库

```powershell
uv run wechat-oracle init-db
```

### 4. 跑

三个终端分别跑：

```powershell
# Terminal 1 — 实时抓
uv run wechat-oracle ingest live

# Terminal 2 — 命令调度（自动回复）
uv run wechat-oracle dispatcher

# Terminal 3 — 用大号在群里 @ 小号
# @<小号> /help
# @<小号> 谁今天提到了股票？
```

> 要让自动回复工作，**WeChat 主窗口必须可见**（不能在托盘里）。dispatcher 启动时会用 wx4py 的 `get_group_nickname` 验证当前登录账号在每个群的昵称，跟 `WO_BOT_NAME` 对不上时 warning（不阻断；可能你登错号了）。

---

## 命令详解

### `/find` — 语义检索

```
/find [from:<人>|@<人>] [since:YYYY[-MM[-DD]]] <描述>
```

- 不指定人 → 查群内**全员**
- `from:<人>` 限定单人，**推荐**（不会 @ 通知本人）
- `@<人>` 兼容老姿势，会 ping 那人
- `since:` 可选时间下界（年 / 月 / 日均可）
- 多 marker 任意顺序

例子：

```
@小号 /find 关于股票的讨论                          # 全员
@小号 /find from:张三 关于数学的发言                 # 限定张三，不打扰
@小号 /find @张三 关于数学的发言                     # 同上但会 ping
@小号 /find since:2024-01 关于股票
@小号 /find from:张三 since:2024 关于X
```

### `/help` — 查命令

```
@小号 /help          # 命令总览
@小号 /help find     # 看 /find 详细用法
```

### 自由问答兜底

不带 `/` 命令的 @ 消息直接进入兜底：把最近 5000 条群消息当上下文，让 LLM 直接回答（条数受 `WO_DISPATCHER_CONTEXT_CHAT` 控制）。

```
@小号 谁今天提到了股票？
@小号 帮我总结一下昨晚的讨论
@小号 张三最近在忙什么
```

> `/find` 和自由问答的检索池**包含合并转发包里的子项**（参见 [合并转发](#合并转发-merged-forward)）。即「张三 2024 年说过的话被 2026 年某人转发进群」也搜得到。

### 错误反馈

写错命令不再静默——直接收到错误提示 + 该命令的帮助：

- `@小号 /find` → ⚠️ 缺参数 + `/find` 用法
- `@小号 /xyz` → ⚠️ 未知命令 + 命令总览
- `@小号 /find since:badformat 关于X` → ⚠️ since 格式说明

---

## 数据流细节

### 实时抓（`ingest live`）

- 订阅 WeFlow `/api/v1/push/messages` SSE 流
- 每条 `message.new` 触发一次 `/api/v1/messages` 拉完整记录后写库
- watermark 从启动那一刻开始 → **只抓启动以后的消息**，历史靠回灌
- 启动时拉一次 `/api/v1/group-members` 建 `wxid → 群昵称` 映射，写消息时填 `sender_display`
- 断线指数退避重连（1s → 60s 封顶）
- 单群异常不会影响其它群

### 历史回灌（`ingest backfill`）

朋友在 WeFlow：选群 → 导出 → JSON 格式 → **整个导出文件夹打成 zip 发给你**。约定的目录结构：

```
群聊_xxx/                  ← 解压根
├── texts/
│   └── 群聊_xxx.json      ← 你导入时指给这个 .json
├── images/                ← JSON 里 `../images/...` 的目标
├── voices/
└── videos/
```

JSON 里媒体路径是父级相对路径（`../images/...`），需要保留这个结构才能定位。导入命令：

```powershell
uv run wechat-oracle ingest backfill 群聊_xxx\texts\群聊_xxx.json --format weflow
```

支持的格式：
- `weflow` — WeFlow 原生 JSON 导出
- `jsonl` — 每行一个规范化 `Message`，调试管道用

**媒体处理**：被引用的媒体会复制进 `data/media/<group_id>/<kind>/<filename>`（kind ∈ `images / voices / videos / stickers`）。DB 里 `media_path` 存相对 `data_dir` 的路径。**导入完成后源文件夹可以删**。

媒体缺失时（朋友只发 .json）：导入照常成功，`media_path` 留空，`content_text` 标 `[图片缺失]` / `[语音缺失]` 等。

### 合并转发 (merged-forward)

WeFlow 把微信「合并转发的聊天记录」当 `localType = (19 << 32) | 49` 推过来，`rawContent` 里 `<recorditem>` 含完整子消息列表。我们的处理：

- 外层 wrapper 落 `messages` 表，`type='forward'`，`content_text='[聊天记录]'`
- 子项落独立的 `forwarded_records` 表，每行带 `parent_msg_id` 反指 wrapper
- **dispatcher 检索时把两表 UNION**，候选 ID 加前缀（`m:` 直发消息 / `f:` 转发子项）区分
- 子项的 `t` 是**原消息时间**（`<srcMsgCreateTime>`），不是被转发进群的时间——`since:2024` 这类查询能正确命中老内容

不解析的边界（写在前面省得踩坑）：
- 嵌套合并转发（`<dataitem datatype="17">`）只存占位 `[聊天记录]`，**不递归**
- 非文本子项（图片 / 视频 / 文件 / 链接）只存占位（`[图片]` 等），不下载媒体
- 子项的发送者用 `<sourcename>`（显示名），WeChat XML 里的 `<hashusername>` 是 sha256 不可逆，没法回填 wxid

详细字段语义见 `models.py` 里 `ForwardedItem` 的 docstring。

### 命令调度（`dispatcher`）

```
SQLite poll  ──►  parse_command (regex + dispatch)
                       │
              ┌────────┼─────────┐
          Command   ParseError  None
              │         │        │
              ▼         ▼      (silent)
          execute   reply with
          (LLM)     error+help
              │         │
              └────┬────┘
                   ▼
            stdout / log / wx4py send
                   │
                   ▼
            UPDATE command_runs
```

- 每 3s 扫 DB 找 `@<bot>` 文本消息（`LIKE %@<bot>%` 粗筛）
- 命中后 Python 端用 `parse_command` 三态 dispatch：`Command` / `ParseError` / `None`（非命令静默）
- 命令处理记录在 `command_runs` 表（msg_id 作主键），重启不重跑
- **启动跳过积压**：dispatcher 启动时把所有未处理的历史 `@<bot>` 一次性写入 `command_runs(status='ok', result='(startup-skip)')`，避免冷启动 / 大批量回灌后向群里灌一通陈年答复。只对启动后新到的消息回复。

### `/find` 检索流水线

```
SQL 粗筛 (group + sender + since + 排除 bot/命令)
          │
          ▼  最多 500 条候选
DeepSeek (system prompt 强调字面命中必算 + 同时返回 keywords)
          │
   命中 ≥ 1?
          │
    ┌─────┴─────┐
   yes          no  ──►  keyword fallback (SQL substring)
    │                       │
    └───── 命中 / 兜底 ─────┘
                │
                ▼
         格式化 → stdout / log / 群内回复
```

**关键词兜底**：避免小模型对边缘相关过严漏掉字面命中。LLM 命中为空时按它返回的关键词跑一遍 SQL `LIKE`。

### 自由问答兜底

`@<bot> <无 / 命令的文本>` → `ChatCommand`：

- 拉最近 `WO_DISPATCHER_CONTEXT_CHAT` 条（默认 5000）群消息（任意 sender，排除 bot 自己 + `/` 命令消息）
- 喂 chat-assistant prompt（强调"宁缺勿编、控制 2-6 句、不复制原文"）
- 直接把 LLM 回复发回群

---

## 配置参考

所有变量走 `pydantic-settings`，前缀 `WO_`。可在 `.env` 或环境变量里设。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WO_DATA_DIR` | `data` | 数据根目录 |
| `WO_DB_PATH` | `data/wechat-oracle.db` | SQLite 文件路径 |
| `WO_MEDIA_DIR` | `data/media` | 媒体目录 |
| `WO_GROUPS` | `[]` | 监听的群（群名 / 备注 / wxid，逗号或 JSON 数组） |
| `WO_LOG_LEVEL` | `INFO` | loguru 级别 |
| `WO_WEFLOW_BASE_URL` | `http://127.0.0.1:5031` | WeFlow HTTP API |
| `WO_WEFLOW_TOKEN` | — | WeFlow access token |
| `WO_BOT_NAME` | — | 小号在群里的群昵称（dispatcher 必填） |
| `WO_DEEPSEEK_API_KEY` | — | DeepSeek API key |
| `WO_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `WO_DEEPSEEK_MODEL` | `deepseek-v4-pro` | 模型名 |
| `WO_REPLY` | `True` | 是否自动回群里 |
| `WO_REPLY_BACKEND` | `wx4py` | 回复通道：`wx4py`（UI 自动化，默认）/ `openclaw`（HTTP API，**实验性**——群聊 send 未验证）/ `stdout`（不发） |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | dispatcher 扫 DB 间隔（秒） |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` 单次候选上限 |
| `WO_DISPATCHER_CONTEXT_CHAT` | `5000` | 自由问答上下文窗口 |

---

## 数据库 schema

```sql
messages (
    msg_id, wx_msg_id, group_id, group_name,
    sender_wxid, sender_display,
    t,              -- unix seconds
    type,           -- text/image/voice/video/link/forward/quote/sticker/system
    content_text,
    media_path,     -- 相对 data_dir
    reply_to_wx_msg_id, quote_text,
    source,         -- live / backfill
    status,         -- raw / mm_pending / mm_done / assigned / indexed
    dedupe_key,     -- UNIQUE
    created_at
)

command_runs (
    msg_id PRIMARY KEY,    -- references messages.msg_id
    started_at, finished_at,
    status,                -- running / ok / error
    result                 -- short summary; 启动跳过的消息标 '(startup-skip)'
)

forwarded_records (
    id PRIMARY KEY,
    parent_msg_id,         -- → messages.msg_id (CASCADE on delete)
    seq,                   -- 0-based 在 wrapper 内的序号
    sender_display,        -- <sourcename>，无 wxid（<hashusername> 是 sha256）
    t,                     -- <srcMsgCreateTime>，是源消息的时间，不是 wrapper 的时间
    datatype,              -- WeChat dataitem 类型；1=文本，其它存占位
    content,               -- 文本时存正文，其它存 [图片]/[视频]/[聊天记录] 等
    src_msg_id,            -- <fromnewmsgid>，源群里的原 msg_id（informational）
    UNIQUE(parent_msg_id, seq)
)
```

跨源去重靠 `UNIQUE(dedupe_key)`：有 `wx_msg_id` 时用 `wx:<group>:<id>`，没有时用关键字段哈希。`messages.status` 和 `command_runs` 是两条并行的状态机，互不污染。

---

## 调试 / 可观测性

- `data/dispatcher.log` — 每条命令的完整渲染输出，纯文本可读
- `data/llm_debug.log` — **每次 LLM 调用**的 system / user / 原始响应 / 解析后 JSON，10MB 滚动（保留 `.1` 备份），排查"为什么没命中"看这个最直接
- dispatcher 终端实时打印 `candidates=N llm_hits=M keywords=[...] fallback=true/false`

```powershell
uv run wechat-oracle status   # DB 路径 / 总条数 / 按状态分布 / 按群分布
```

---

## 实验进行中：openclaw 群聊回发可行性

腾讯通过 OpenClaw 开放了官方的 [iLink Bot HTTP API](https://github.com/hao-ji-xing/openclaw-weixin)，跨平台、零封号风险。一系列实验逐步验证它能不能替掉 wx4py。

**已确认**（2026-05）：
- ✅ DM 双向通：bot 收 user→bot 私聊，发 bot→user 也成。`from_user_id` 用 `@im.wechat` namespace，`to_user_id` 用 `@im.bot`。
- ❌ Bot **不能加进群**：登录后 bot 只在微信里表现为一个对话窗口，没有「邀请到群」入口。

**还没确认**：bot 不在群里，**API 层是否仍能向某个具体 group_id 推送消息**？这是我们现在要测的——往 `xxx@chatroom` 发 `sendmessage`，看 server 接不接、群里看不看得到。

CLI 流程：

```powershell
# 1. 一次性登录（QR 在终端显示）
uv run wechat-oracle openclaw login

# 2. probe 看私聊收到的字段长啥样（确认 token 工作）
uv run wechat-oracle openclaw probe --minutes 3

# 3. 群聊回发实验（拿一个你能控制的小群的 wxid）
uv run wechat-oracle openclaw send --group-id "<xxx@chatroom>" "test from openclaw"
uv run wechat-oracle openclaw send --to-user "<xxx@chatroom>" "test alt path"
```

**结果分类**：

| HTTP 响应 + 群里观察 | 含义 | 下一步 |
|---|---|---|
| 2xx + 群里出现消息 | 群发可行 | 把 wx4py 替换计划提上日程 |
| 2xx + 群里没消息（黑洞） | server 接但不投递 | 留 wx4py |
| 4xx + `not_in_group` 类错误 | 必须先入群 | 找入群入口；找不到就留 wx4py |
| 4xx + `invalid_target` 类错误 | bot 不能寻址 group | 路堵，留 wx4py |

## 已知边界 / 限制

- **Windows + 中文微信 UI** 是默认开发路径；英文 UI（国际版 WeChat）下 wx4py 控件名匹配可能挂
- **微信主窗口必须可见**才能让 wx4py 发消息；最小化到托盘 → 失败
- **wx4py 跟微信版本耦合**——4.1.x 测过，再后续版本可能要等 wx4py 跟进
- **dedupe 边缘情况**：没有 `wx_msg_id` 的消息（系统消息等）走 fallback 哈希；media 消息两条管道编码不同（live 用 `mediaLocalPath` 原值，backfill 用相对路径）→ 同一条可能重复
- **live 启动前的消息抓不到**——历史只能靠 backfill；自由问答兜底受此影响（启动后才有 context）
- **图片 / 语音 / 视频** 目前只存路径不做内容理解（OCR / ASR / caption 都没接）

---

## 开发

```powershell
uv sync
uv run wechat-oracle init-db
uv run wechat-oracle <subcommand>

# Enable the project's pre-commit hooks (one-time per clone):
git config core.hooksPath .githooks
```

Pre-commit gates（见 `.githooks/pre-commit`）：

1. **Doc-sync 契约**——dispatcher.py / schema.sql / config.py / cli.py 中任何一项命中 marker（命令类常量、CREATE TABLE、Settings 字段、@app.command），README.md 必须在同一 commit 一起改
2. **schema.sql 必须能 parse**——`sqlite3 :memory:` dry-run
3. **Python 语法**——staged `.py` 跑 `py_compile`
4. **API key 防泄露**——staged diff 里出现 `sk-...` 串直接拒
5. **`.env` / `*.key` / `*.pem`** 一律不准 staged

紧急绕过：`git commit --no-verify`（别养成习惯）。

代码组织：

```
src/wechat_oracle/
├── cli.py              # typer 入口，子命令注册
├── config.py           # pydantic-settings，所有 WO_ 环境变量
├── models.py           # Message dataclass + dedupe_key 计算
├── db.py               # SQLite 连接 + transaction 上下文
├── schema.sql          # DDL（messages, command_runs, group_state, schema_meta）
├── dispatcher.py       # 命令解析 + LLM + wx4py 发送
└── ingest/
    ├── backfill.py     # WeFlow JSON 导入 + 媒体复制
    ├── live.py         # SSE 订阅 + group-members 富化
    ├── forwarded.py    # 合并转发 rawContent 解析
    └── writer.py       # 唯一写入路径，UNIQUE dedupe
```

新增 dispatcher 命令：在 `dispatcher.py` 里写 `Command` 子类 + `@register`，自动进 `/help` 列表。

> ⚠️ **改命令 / schema / 配置 / CLI 必须同时同步 README**——这些都是双重 / 多重存储的事实，README 是给人看的对外文档，没人帮你校验一致性。详见 `CLAUDE.md` 「易漂移点速查」表 + 「命令体系维护契约」。仓库里 `.claude/hooks/check_doc_sync.py` 是 PostToolUse hook 自动 backstop（覆盖 dispatcher.py / schema.sql / config.py / cli.py 这四个常改文件）。

---

## 致谢

- [WeFlow](https://github.com/hicccc77/WeFlow) — 解密本地 WeChat DB 并提供 HTTP API
- [wx4py](https://github.com/claw-codes/wx4py) — Windows UI 自动化驱动 WeChat
- [DeepSeek](https://api.deepseek.com) — OpenAI 兼容的国内 LLM
- [uv](https://docs.astral.sh/uv/) — 飞快的 Python 依赖管理

## License

[MIT](LICENSE)
