# WeChat-Oracle

> 本地化的微信群聊归档 + 可问答助手。把群历史攒成 SQLite，用 LLM 在群里直接 @ 它问问题。

A local-first WeChat group-chat archiver with an LLM-backed in-group Q&A assistant. All chat data stays on your machine; the bot answers questions like _「谁今天提到了股票？」_ or _「张三上次说数学和物理哪个更难？」_ by searching the local archive.

---

## 它能做什么

- **实时抓取**：订阅 [WeFlow](https://github.com/hicccc77/WeFlow) 的 SSE 推送，新消息毫秒级落库
- **历史回灌**：导入 WeFlow 的 JSON 导出，把媒体复制进项目自有的 `data/media/`，一次导入永久脱钩源目录
- **群内问答机器人**：在群里 @ 小号，触发以下几类操作
  - `/find <描述>` — 语义检索群历史（LLM 精筛 + 关键词兜底）
  - `/sum <主题>` — 总结当前群的一段聊天
  - `/recent [N]` — 直接列最近入库消息，不调用 LLM
  - `/balance` — 查询当前 LLM API 账号余额
  - `/ask <问题>` — 纯模型问答，不读取群聊上下文，省 token
  - `/explain` — 解释引用消息或给定文本
  - `/help` — 查命令
  - **自由问答**（无 `/` 命令）：把最近 2500 条群消息当上下文，让 LLM 直接答
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
        │              ▼
        │       ingest live 进程
        │       ├─ 主线程: 写 messages 表
        │       └─ mm 工作线程: OCR / ASR → 写 transcript
        │              │
        └─► backfill ──┴────► SQLite (data/wechat-oracle.db, WAL)
                                        │
                                        ▼
                                  dispatcher ──► LLM (OpenAI-compatible)
                                        │              │
                                        ▼              ▼
                                  data/dispatcher.log  data/llm_debug.log
                                        │
                                        ▼
                                  wx4py 把回复打回群里
```

两个常驻进程跑在 WAL 模式的同一份 SQLite 上，一次性 / 调试入口若干：

| 进程 | 职责 | 入口 |
|---|---|---|
| `ingest live` | SSE 订阅 + 写库 + 内嵌 mm 工作线程跑 OCR/ASR 填 `transcript` | `uv run wechat-oracle ingest live` |
| `dispatcher` | 检测命令 → LLM / agent loop → 群里回复 | `uv run wechat-oracle dispatcher` |
| `ingest backfill` | 一次性导入历史 | `uv run wechat-oracle ingest backfill <file>` |
| `worker mm` | 单独跑 OCR/ASR worker（一般不用——`ingest live` 已经包了；想 backfill 后 catch up 或单独调试时用） | `uv run wechat-oracle worker mm` |

---

## 前置条件

- **Windows 10/11**（wx4py 走的 UI 自动化只支持 Windows）
- **微信 PC 4.1.x（Qt 版）** — wx4py 实测 4.1.7.59 / 4.1.8.29，4.1.9.30 可用；中文 UI 推荐
- **WeFlow 桌面端** — 装好并能解密你的 WeChat 数据；启动 HTTP API 服务
- **Python 3.12+**，**[uv](https://docs.astral.sh/uv/)** 管理依赖
- **LLM API Key**（dispatcher 走 OpenAI 兼容接口）

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
WO_LLM_API_KEY=sk-...
# WO_LLM_ENDPOINT=https://api.deepseek.com
# WO_LLM_MODEL=deepseek-v4-pro
# WO_LLM_MAX_TOKENS=1000               # 默认输出上限；chat/sum/short 可分别覆盖
# WO_REPLY=0                           # 默认 1 = 自动回到群里；0 = 只本地输出

# 视觉模型（可选 — 让 chat / /explain / /ask 能读图原文，详见「视觉模型集成」段）
# WO_VISION_API_KEY=sk-...             # DashScope 兼容 key；空 = 关闭功能，纯 OCR 文本
# WO_VISION_MODEL=qwen-vl-plus         # 默认；强推荐 qwen3-vl-plus 或 qwen3.6-plus（OCR 更强）

# 多媒体识别（可选 — mm worker 的 ASR 模型档位；mm 跟 ingest live 同进程跑）
# WO_WHISPER_MODEL=small               # tiny|base|small|medium|large-v3，默认 small

# Agent loop（@<bot> chat 路径，已默认启用）
# WO_AGENT_BASE_PROBABILITY=0.0        # >0 让 bot 偶尔自主插话（默认 0 = 仅 @ 触发）
# WO_AGENT_REFLECTION_ENABLED=True     # 关闭跳过 Phase B（不写任何 member/group/persona 笔记）
```

### 3. 建库

```powershell
uv run wechat-oracle init-db
```

### 4. 跑

两个终端：

```powershell
# Terminal 1 — 实时抓 + OCR/ASR 后台识别（mm 工作线程自动跟 live 一起起）
uv run wechat-oracle ingest live

# Terminal 2 — 命令调度（自动回复）
uv run wechat-oracle dispatcher

# 然后用大号在群里 @ 小号
# @<小号> /help
# @<小号> 谁今天提到了股票？
# @<小号> 引用某条图片消息后 → /explain 或 /ask 这是什么
```

> - **要让自动回复工作，WeChat 主窗口必须可见**（不能在托盘里）。dispatcher 启动时会用 wx4py 的 `get_group_nickname` 验证当前登录账号在每个群的昵称，跟 `WO_BOT_NAME` 对不上时 warning（不阻断；可能你登错号了）。
> - **`ingest live` 首次跑会按需下载 RapidOCR / faster-whisper 模型权重**（几十 MB ~ 几 GB，看 `WO_WHISPER_MODEL` 档位）——OCR/ASR 引擎是懒加载，群里第一张图 / 第一条语音才触发；纯文本流量下不消耗。
> - **想单独跑 mm**（比如导入历史 backfill 后 catch up，或不开 live 时调试 OCR/ASR）：`uv run wechat-oracle worker mm`。日常部署不需要。
> - **配了 `WO_VISION_API_KEY` 后才有图片直读能力**——agent 在 chat 自由问答里会用 `read_image` 工具直接看图，`/explain` 和 `/ask` 引用图片时会把字节直接喂视觉模型。没配也能跑，但群里图片只看得到 OCR 文本（残的就是残的）。

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

`/help` 总览只显示每条命令的一行说明和用法；需要例子时用 `/help <命令>`。

### `/sum` — 总结当前群

复用当前群的候选消息池，让 LLM 总结一段聊天。可用 `from:` 限定发言人、`since:` 限定时间下界、`limit:` 控制最多读取多少条。

```
@小号 /sum
@小号 /sum 今天讨论了什么
@小号 /sum since:2026-05-01 关于装修
@小号 /sum from:张三 limit:100
```

### `/recent` — 查看最近入库消息

不调用 LLM，直接返回当前群最近 N 条可见消息。适合排查 live 是否抓到了消息、bot 上下文里大概会看到什么。

```
@小号 /recent
@小号 /recent 20
```

### `/balance` — 查询 LLM 余额

不调用 LLM，直接请求当前 `WO_LLM_ENDPOINT` 对应的 DeepSeek 兼容余额接口。若 endpoint 以 `/v1` 结尾，会自动退回根路径后请求 `/user/balance`。

```
@小号 /balance
```

### `/ask` — 纯模型问答

不读取群聊历史，只把问题本身发给 LLM。适合翻译、改写、解释概念、写短文本这类不需要群聊上下文的场景，比自由问答兜底省 token。

```
@小号 /ask 帮我把这句话改得更礼貌：今晚别迟到
@小号 /ask SQLite WAL 是什么？
```

> 引用一条消息再 `/ask` 时：
> - **文本/链接/卡片等**：被引用内容前置进 prompt，作为问题的一部分送进文本模型（例如「@小号 /ask 这句话什么意思」+ 引用一段文字）
> - **图片**：配置了 `WO_VISION_API_KEY` 时直接走 vision 单轮直读（跟 `/explain` 同一条路径），把字节喂视觉模型而不是 `[图片]` 占位。视觉关闭时降级回文本路径，模型只能看到 `[图片]`

### `/explain` — 解释引用消息或文本

不读取群聊历史。引用一条消息后发送 `/explain`，会只解释被引用内容；也可以直接在命令后写待解释文本。

```
@小号 /explain
@小号 /explain 这句话是什么意思：SQLite 开了 WAL
```

> **图片直读**：如果引用的是一张图片消息，且配置了 `WO_VISION_API_KEY`，会跳过 OCR 文本路径直接把图片字节喂视觉模型。无需 sentinel 协议——用户已经明确指定哪张图。视觉关闭或图片文件不存在时降级回文本路径，看到的就是 `[图片]` 占位。

### 自由问答兜底（多轮 agent loop）

不带 `/` 命令的 @ 消息进入 agent loop——多轮 tool-calling，自己决定怎么答：

```
@小号 谁今天提到了股票？
@小号 帮我总结一下昨晚的讨论
@小号 张三最近在忙什么
```

agent 看到的「初始上下文」只有最近 `WO_AGENT_RECENT_CONTEXT_CHAT`（默认 50）条群消息——比老路径的 2500 条小一个量级，token 开销低。要看更多历史就**主动调工具**：

- `recall_group_history(query, since_days?, sender_wxid?)`：在本群历史里 substring 搜（`content_text` + `transcript` 都搜）
- `view_quoted_chain(msg_id)` / `expand_forward_bundle(msg_id)`：跟引用链上溯 / 展开合并转发的子项
- `read_image(msg_id, prompt?)` / `read_voice(msg_id)`：让视觉模型直接看一张图 / 拿语音转录（已转写的秒返）
- `read_group_memory()`：取群文档（成员、事件、群文化全在一段 freeform text 里，agent 自己组织结构）
- `stay_silent(reason)`：判断这次不该回应，闭嘴（实际不会发回群）

回答完，进入 **Phase B 反思**（`WO_AGENT_REFLECTION_ENABLED=True` 时）：agent 看自己刚才做了什么，决定要不要把新观察写进 `group_memory`（替换语义，100k 上限）或 `persona_drift`（替换语义）。详见「数据流细节」段。

> agent 触发的检索池**包含**：直发文本 + **引用回复**（用户的回复正文）+ **合并转发包里的子项**（详见 [appmsg 子类型](#appmsg-子类型-localtype49-family)）+ **图片/语音的 OCR/ASR 文字**（详见 [多媒体识别](#多媒体识别-worker-mm)）。即「张三 2024 年的话被 2026 年某人转发进群」、「李四引用某人发言后说了啥」、「上周谁分享的那张报表显示啥」都搜得到。`/find` 用同一份检索池做 LLM 精筛。

### 多媒体识别（mm worker，跟 live 同进程跑）

把图片 / 语音里的文字识别出来填进 `messages.transcript`，让 dispatcher 检索时能看到内容而不只是 `[图片]` 占位：

- **OCR**：[rapidocr-onnxruntime](https://github.com/RapidAI/RapidOCR)（PP-OCRv4 ONNX，中文友好），CPU 上 ~1s/张
- **ASR**：[faster-whisper](https://github.com/SYSTRAN/faster-whisper)，默认 `small` 模型，CPU 上接近实时；设 `WO_WHISPER_MODEL=tiny|base|medium|large-v3` 可覆盖
- 两个模型**全本地跑**，识别内容不出本机；引擎都是**懒加载**，群里第一张图 / 第一条语音才触发模型权重下载和 RAM 占用
- 处理顺序：按消息时间倒序（新的优先），队列空时 30s sleep
- 三种状态：`transcript IS NULL`（待处理）/ `transcript=''`（处理过没识别出文字 / 文件丢了——不重试）/ `transcript='<text>'`（成功）
- 出口：`fetch_candidates` 的 SQL CASE 优先用 `transcript`，形状 `[图片] <识别文字>` / `[语音] <转录>`，跟 `[链接]` 等占位前缀一脉相承

部署：默认在 `ingest live` 里以 daemon 线程跑——你启动 `uv run wechat-oracle ingest live` 就同时启动了 mm worker，**不用单独再开一个进程**。它和 live 主线程各持有自己的 SQLite 连接，靠 WAL 隔离写。要单独跑（比如导入完 backfill 后追识别、或者调试 OCR/ASR 时不想拉 live 流量）：

```powershell
uv run wechat-oracle worker mm
```

> ⚠️ **历史 backfill 行没有 media 文件**：从 WeFlow JSON 导出的图片 / 语音如果没带媒体目录（`media_path IS NULL`），worker 跳过——文件本就在 WeFlow 那边没复制过来，这些行永远空白。**只有 live 抓的（媒体存 WeFlow cache 绝对路径）能识别**。要补全历史，需要重新跑一次带 `media=1` 的 backfill。

#### 视觉模型集成（可选）

`worker mm` 的离线 OCR 是**检索的主索引**——所有历史问答都依赖 `transcript` 文本。但 PP-OCRv4 在截图模糊、表格、手写、复杂版面这些场景下会漏字。开启 `WO_VISION_API_KEY` 后，dispatcher 在三个入口可以直接读原图：

**1. chat agent loop → `read_image` 工具**

agent 在 Phase A 看到 `[图片]` 或 `[图片·OCR] <可疑残文>` 时可以主动调 `read_image(msg_id, prompt?)`，把图字节喂视觉模型（默认 Qwen-VL-Plus via DashScope），把视觉模型的描述放进 trace 继续推理。模型决定要不要看、看哪张——不需要 sentinel 协议。

**2. `/explain` 引用图 → 单轮直读**

用户已经明确指定了哪条消息，图片字节直接喂视觉模型，按 `/explain` 的「简明解释」语气作答。

**3. `/ask` 引用图 → 单轮直读**

同 `/explain` 但走 `/ask` 的「轻量问答」语气；适合「翻译这张图里的文字」「这道题怎么解」「这个截图的报错什么意思」。

**通用特性**：
- **完全可插拔**：`WO_VISION_API_KEY` 为空时整个 vision 功能关闭。agent 调 `read_image` 会拿到一个明确的 `ToolError`（"vision 未配置，回退去 recall_group_history"）；`/explain` / `/ask` 引用图会降级到 `[图片]` 占位
- **硬上限 `WO_VISION_MAX_IMAGES=3`**：单次 vision 请求最多附 3 张图（agent `read_image` 一次只送 1 张，但工具可被多次调用——这条是 vision client 层的最终兜底）
- **降级安全**：图文件不存在 / 视觉调用失败 → ToolError 给 agent / 兜底文案给 /explain & /ask，用户能感知到失败但不会拿到瞎编内容
- **只在用户明确问图或 agent 主动判断需要时起作用**：`/find` / `/sum` / `/recent` 不走视觉，永远纯文本

**模型选择**（`WO_VISION_MODEL`）：
- `qwen-vl-plus`（默认，Qwen2.5-VL）— 便宜，普通截图够用
- `qwen3-vl-plus` — Qwen3-VL，OCR / 文档解析显著更强，**生产推荐**
- `qwen3.6-plus` — 最新最强，复杂版面 / 数学公式 / 手写时再考虑

### 错误反馈

写错命令不再静默——直接收到错误提示 + 该命令的帮助：

- `@小号 /find` → ⚠️ 缺参数 + `/find` 用法
- `@小号 /sum since:badformat` → ⚠️ since 格式说明
- `@小号 /recent abc` → ⚠️ N 必须是正整数
- `@小号 /balance x` → ⚠️ `/balance` 不需要参数
- `@小号 /xyz` → ⚠️ 未知命令 + 命令总览

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

### appmsg 子类型 (localType=49 family)

WeChat 把所有 `<appmsg>` 包裹的消息（链接卡片 / 文件 / 视频号 / 红包 / 引用回复 / 合并转发……）都用 `localType=49` 表达，区分靠 XML 里的 `<appmsg>.<type>`。WeFlow 把这个值偷塞进 `localType` 高 32 位：`localType = (appmsg.type << 32) | 49`。

我们对每个 subtype 单独决策（[forwarded.py](src/wechat_oracle/ingest/forwarded.py) 模块 docstring 有完整表）：

| appmsg.type | 含义 | 我们的归类 | content_text |
|---|---|---|---|
| 19 | 合并转发的聊天记录 | `forward` + `forwarded_records` 子表 | `[聊天记录]` |
| **57** | **引用回复**（用户加自己的话回原消息） | **`quote`** | **用户的回复正文（`<title>`）**；`quote_text` 存被引内容、`reply_to_wx_msg_id` 存被引消息的 svrid |
| 4 / 5 | 链接 / 文章卡片 | `link` | `[链接] 标题\nURL` |
| 6 | 文件 | `link` | `[文件] 文件名` |
| 62 | 视频号短视频 | `link` | `[视频号] 标题\nURL` |
| 51 | 视频号 feed（旧版本不支持） | `link` | `[视频号]` |
| 8 | 表情商店 / 微信豆 | `link` | `[表情/卡片]` |
| 2000 | 转账 | `link` | `[转账 ¥99.99] {留言}`（金额 + memo） |
| 2001 | 红包 | `link` | `[红包: 恭喜发财]`（祝福语） |
| 其它 | 未识别 appmsg | `link` | `[卡片]` 或 WeFlow 原始 `content` 兜底 |

**`/find` 和 chat 共用一个 `fetch_candidates`，都是全量视野**——所有 type 都进候选池，图片/语音/视频/sticker 缺正文时用 `[图片]`/`[语音]`/`[视频]`/`[表情]` 占位，链接卡片用 `[链接] 标题\nURL`，转账带金额（`[转账 ¥99.99]`），红包带祝福语（`[红包: 恭喜发财]`）。LLM 自己能识别占位符不当作主题词。

唯一差别由 `for_chat` 控制：**chat 模式保留 `@<bot> /xxx` 命令消息**（它们是对话流程的一部分："然后我让 bot 查了 X..."），**`/find` 模式排除**它们（其它人之前的 `/find` 调用不算当前查询的相关信号）。

#### 合并转发的特别处理

合并转发包（appmsg.type=19）的子消息会落进独立的 `forwarded_records` 表：

- 外层 wrapper 落 `messages` 表，`type='forward'`，`content_text='[聊天记录]'`
- 子项每行带 `parent_msg_id` 反指 wrapper
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
LLM (system prompt 强调字面命中必算 + 同时返回 keywords)
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

### 自由问答兜底（agent loop）

`@<bot> <无 / 命令的文本>` → `ChatCommand` → `chat_via_agent` → `agent.runtime.run_agent`：

**Phase A（read-only，最多 `WO_AGENT_MAX_STEPS`=10 步）**

1. 装配 system prompt：`agent/persona.py` 把 `data/personas/<group_id>.yaml`（静态人格核心）+ `persona_drift` 表（agent 自维护的演化补充）+ 操作规则（工具清单 + msg_id 整数约定 + 简短输出风格）拼起来
2. 装配 user message：当前时间 + 触发原因 + 引用消息（如有）+ 最近 `WO_AGENT_RECENT_CONTEXT_CHAT`（50）条群消息（每行 `[msg_id] (time) sender (wxid): 内容`）+ 用户对你说的话
3. 进 tool-calling 循环：每步模型可以调一组只读工具（recall / view_quoted / expand_forward / read_image / read_voice / read_group_memory / stay_silent）或输出最终回复文字。每步的 tool name + args + result 都进 trace
4. 终态：模型输出文字 → 是回复；调 `stay_silent` → 标记沉默；用满步数 → 取最后一条 assistant content 兜底

**Phase B（反思，`WO_AGENT_REFLECTION_ENABLED=True` 时；最多 `WO_AGENT_REFLECT_MAX_STEPS`=5 步）**

5. 模型看完整 Phase A trace + 最终回复，决定要不要 update 记忆。可调：读 `read_persona_drift` / `read_group_memory`（先读再写），写 `update_persona_drift`（替换） / `update_group_memory`（替换，100k 上限）
6. 终态：模型输出空文本 = 不写；用满步数 = 强制结束。绝大部分对话 Phase B 应该 0 写入

**两阶段都结束后**：

7. 写一行 `agent_run_log`：group_id / trigger_msg_id / trigger_kind / phase_a_trace JSON / phase_b_trace JSON / reply_text / 时间戳——审计 + 调试主入口。如果 Phase B 写了 persona_drift / group_memory，对应行的 `last_run_id` 反向链回这次 run（防 summary drift 不可追溯）
8. `reply_text` 非空 → 走 `replier.send` 发回群；空（`stay_silent`）→ dispatcher 跳过 send，群里看不到 bot 任何反应

**触发**：dispatcher 现在扫**所有未处理的 live 消息**（不只 @ 的），在 `_classify_trigger` 里分三类——

| kind | 条件 | cooldown |
|---|---|---|
| `mention` | `content_text` 含 `@<bot_name>` | 不受 cooldown 限制 |
| `reply` | quote-reply 的 parent (`reply_to_wx_msg_id`→`wx_msg_id`) 是 bot 自己 | 不受 cooldown 限制 |
| `probability` | 上面都不是 + `WO_AGENT_BASE_PROBABILITY > 0` 摇到 + 上次说话超过 `WO_AGENT_COOLDOWN_SECONDS` | **受 cooldown 限制** |
| `None` | 上面都没命中 → `_finalize` 标 `(no-trigger)` 跳过，不烧 LLM | — |

`mention` 走完整 `parse_command` 流程（slash 命令都能用，纯文本进 `ChatCommand`→agent）；`reply` / `probability` 直接进 agent loop（无 slash 解析），因为它们没有"命令语法"概念。

reply 触发依赖知道 bot 自己的 wxid。两种来源：(1) `WO_BOT_WXID` 配置（手填）；(2) **自动发现**——dispatcher 启动 + 每 5 个 poll 周期重试：`SELECT sender_wxid FROM messages WHERE sender_display=WO_BOT_NAME ...`。只要 WeFlow SSE 把 bot 自己的回复回流到 `messages` 表（生产路径上应该是），第一次回复后下一轮 poll 就发现了。回流不工作时 reply 触发降级（mention + probability 不受影响），日志里有清晰 warning。`uv run wechat-oracle verify roundtrip` 可以一键查这件事。

**隔离**：所有 SQL 写死 `WHERE group_id=?`，参数从 `GroupScopedTools.__init__` 锁定——`group_id` 不暴露给 LLM 当 tool 参数，从根上禁止跨群泄密。详见 `agent/tools.py`。

### 纯模型问答

`@<bot> /ask <问题>` → `AskCommand`：

- 不查 DB，不调用 `fetch_candidates`
- 不塞最近群聊上下文，只发送当前问题和当前时间
- 引用文本/链接消息 → 内容前置进 prompt
- 引用图片 + `WO_VISION_API_KEY` 配着 → 单轮直读（字节喂视觉模型，跳过文本路径）
- 适合通用知识、翻译、改写、生成短文本，token 成本固定且更低

### 摘要 / 最近消息 / 余额 / 解释

- `@<bot> /sum ...` → `SumCommand`：复用 `fetch_candidates`，只看当前群，可用 `from:` / `since:` / `limit:` 收窄后让 LLM 总结
- `@<bot> /recent [N]` → `RecentCommand`：复用 `fetch_candidates`，但不调用 LLM，直接渲染最近消息
- `@<bot> /balance` → `BalanceCommand`：不调用 LLM，GET `<WO_LLM_ENDPOINT root>/user/balance` 查询账号余额
- `@<bot> /explain ...` → `ExplainCommand`：不查 DB；引用图片 + vision 配着走单轮直读（同 `/ask`），否则解释引用文本或命令后文本

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
| `WO_BOT_WXID` | — | 小号自己的 wxid（可选）。空时 dispatcher 从 `messages` 表里自动发现（看 `sender_display=WO_BOT_NAME` 的回流消息）；要等 WeFlow SSE 回流第一条 bot 消息后才能命中。手填可立刻启用 reply-to-bot 触发 |
| `WO_LLM_PROVIDER` | `openai-compatible` | LLM 适配器；目前实现 OpenAI 兼容接口 |
| `WO_LLM_API_KEY` | — | LLM API key |
| `WO_LLM_ENDPOINT` | `https://api.deepseek.com` | OpenAI 兼容端点；供应商要求时带 `/v1` |
| `WO_LLM_MODEL` | `deepseek-v4-pro` | 模型名 |
| `WO_LLM_JSON_MODE` | `native` | `/find` JSON 返回模式：`native` 传 `response_format`，`prompt` 只靠 prompt 约束 |
| `WO_LLM_MAX_TOKENS` | `1000` | LLM 输出 token 上限默认值 |
| `WO_LLM_CHAT_MAX_TOKENS` | — | 自由问答输出上限；不设则用 `WO_LLM_MAX_TOKENS` |
| `WO_LLM_SUM_MAX_TOKENS` | — | `/sum` 输出上限；不设则用 `WO_LLM_MAX_TOKENS` |
| `WO_LLM_SHORT_MAX_TOKENS` | — | `/ask` / `/explain` 输出上限；不设则用 `min(WO_LLM_MAX_TOKENS, 800)` |
| `WO_REPLY` | `True` | 是否自动回群里 |
| `WO_REPLY_BACKEND` | `wx4py` | 回复通道：`wx4py`（UI 自动化，默认）/ `stdout`（不发）。openclaw 实测不可用，见下方「实验记录」段 |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | dispatcher 扫 DB 间隔（秒） |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` 单次候选上限 |
| `WO_DISPATCHER_CONTEXT_CHAT` | `2500` | 自由问答上下文窗口 |
| `WO_VISION_PROVIDER` | `openai-compatible` | vision 适配器 |
| `WO_VISION_API_KEY` | — | 视觉模型 API key；空 = 关闭功能（chat 退化成纯文本） |
| `WO_VISION_ENDPOINT` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 默认指向 DashScope 兼容模式 |
| `WO_VISION_MODEL` | `qwen-vl-plus` | 视觉模型名 |
| `WO_VISION_MAX_IMAGES` | `3` | 单次 vision 请求最多附几张图（防失控烧钱） |
| `WO_VISION_MAX_TOKENS` | `800` | 视觉调用输出上限 |
| `WO_WHISPER_MODEL` | `small` | `worker mm` 的 ASR 档位：`tiny`/`base`/`small`/`medium`/`large-v3`；越大越准越慢越占内存 |
| `WO_AGENT_BASE_PROBABILITY` | `0.0` | 非 @ 触发时的概率门槛（v0 默认 0 = 仅 @ 触发；>0 让 bot 偶尔自主插话） |
| `WO_AGENT_COOLDOWN_SECONDS` | `300` | bot 自己说完后这么久内不被概率触发 |
| `WO_AGENT_MAX_STEPS` | `10` | Phase A（read-only tools）最多循环步数 |
| `WO_AGENT_REFLECT_MAX_STEPS` | `5` | Phase B（write-only 反思）最多循环步数 |
| `WO_AGENT_REFLECTION_ENABLED` | `True` | 关闭后跳过 Phase B；不写任何 member/group/persona 笔记 |
| `WO_AGENT_PERSONAS_DIR` | `data/personas` | 静态人格 yaml 目录，每群一个 `<group_id>.yaml` |
| `WO_AGENT_RECENT_CONTEXT_CHAT` | `50` | Phase A 初始 system prompt 注入的最近群消息条数 |
| `WO_AGENT_MEMORY_MAX_CHARS` | `100000` | `group_memory` 单文档硬上限；超过 update_group_memory 抛 ToolError，agent 必须先压缩 |

---

## 数据库 schema

```sql
messages (
    msg_id, wx_msg_id, group_id, group_name,
    sender_wxid, sender_display,
    t,              -- unix seconds
    type,           -- text/image/voice/video/link/forward/quote/sticker/system
    content_text,
    media_path,     -- 相对 data_dir 或绝对（live 走 WeFlow cache）
    reply_to_wx_msg_id, quote_text,
    transcript,     -- worker mm 写入的 OCR/ASR 文字；NULL=待处理 / ''=处理过没结果 / '<text>'=成功
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

-- agent loop 记忆 + 审计
persona_drift   ( group_id PK, drift_text, updated_at, last_run_id )           -- 演化人格补充, replace-on-write
group_memory    ( group_id PK, notes_text, size_chars, updated_at,             -- 群文档(成员/事件/文化全放一起),
                  last_run_id )                                                  -- replace-on-write, 100k 上限
agent_run_log   ( run_id PK, group_id, trigger_msg_id, trigger_kind,           -- 每次 agent run 的完整 trace
                  phase_a_trace, phase_b_trace, reply_text,
                  started_at, finished_at )

-- 历史遗留(已不被使用,新装无影响; 旧装可手动 DROP):
-- member_notes / group_notes — 之前按成员 / 按主题分开存的笔记表; 已合并为单一 group_memory
```

跨源去重靠 `UNIQUE(dedupe_key)`：有 `wx_msg_id` 时用 `wx:<group>:<id>`，没有时用关键字段哈希。`messages.status` 和 `command_runs` 是两条并行的状态机，互不污染。

agent 三张表的写语义：`persona_drift` / `group_memory` 都是 replace-on-write（读 → 合并 → 整段写回；100k 上限强制压缩），`agent_run_log` 始终追加。所有 SQL 实现都在 tool 实现层强制 `WHERE group_id=?` —— `group_id` 不暴露给 LLM，靠 `GroupScopedTools` 构造器锁。`persona_drift.last_run_id` / `group_memory.last_run_id` 反向链回 `agent_run_log`，任何笔记内容都能查到是哪次 agent run 写的（防 summarization drift 不可追溯）。

CLI 看 / 改 agent 状态：

```powershell
uv run wechat-oracle agent show <group_id>            # dump persona_drift + group_memory
uv run wechat-oracle agent show-runs <group_id> -n 10 # 最近 N 次 agent_run_log + 写动作高亮
uv run wechat-oracle agent wipe <group_id>            # 清 persona_drift + group_memory（带确认）
uv run wechat-oracle agent wipe <group_id> --persona-only / --memory-only / -y
```

---

## 调试 / 可观测性

- `data/dispatcher.log` — 每条命令的完整渲染输出，纯文本可读
- `data/llm_debug.log` — **每次 LLM 调用**的 system / user / 原始响应 / 解析后 JSON，10MB 滚动（保留 `.1` 备份），排查"为什么没命中"看这个最直接
- dispatcher 终端实时打印 `candidates=N llm_hits=M keywords=[...] fallback=true/false`

```powershell
uv run wechat-oracle status   # DB 路径 / 总条数 / 按状态分布 / 按群分布
```

---

## 实验记录：openclaw 群聊不可行（结论锁定，2026-05）

腾讯通过 OpenClaw 开放了官方的 [iLink Bot HTTP API](https://github.com/hao-ji-xing/openclaw-weixin)，跨平台、零封号风险。本来期望它能替掉 wx4py，但跑下来三层全堵：

1. **微信 UI 层**：登录后 bot 只表现为一个 1-on-1 对话窗口，**没有「加入群聊」/「分享到群」入口**——ClawBot 设计上就不是普通联系人。
2. **SDK 层**：上游 `messaging/inbound.ts` 把 `ChatType` 硬编码成 `"direct"`；`messaging/send.ts` 的 `sendMessageWeixin` 函数签名只接受 `to: string`（→ `to_user_id`），从来不用 `group_id`。
3. **server 层**：实测 `sendmessage` 直接发 `group_id="22810000897@chatroom"`，server 返回 `{"ret": -2}` 拒绝。

✅ 唯一确认能用的：**1-on-1 DM**（user → bot → user）。`from_user_id` namespace `@im.wechat`，`to_user_id` namespace `@im.bot`，发送时必须带 `context_token`。

**所以 `WO_REPLY_BACKEND=openclaw` 不存在**——`OpenclawReplier` adapter 已删除。但底层 `openclaw.py` 模块 + `wechat-oracle openclaw {login,probe,send}` 三条 CLI 子命令保留，作为：

- 诊断工具（万一 Tencent 后续放开了群聊，复跑实验只要 5 分钟）
- 未来如果做「私聊 bot 问知识库」之类 DM-only 场景，HTTP 通道现成

实验复跑命令（仅供后人参考）：

```powershell
uv run wechat-oracle openclaw login                              # QR 登录
uv run wechat-oracle openclaw probe --minutes 3                  # 看 DM 字段
uv run wechat-oracle openclaw send --group-id "<x@chatroom>" "x" # 群发实验
```

## 已知边界 / 限制

- **Windows + 中文微信 UI** 是默认开发路径；英文 UI（国际版 WeChat）下 wx4py 控件名匹配可能挂
- **微信主窗口必须可见**才能让 wx4py 发消息；最小化到托盘 → 失败
- **wx4py 跟微信版本耦合**——4.1.x 测过，再后续版本可能要等 wx4py 跟进
- **dedupe 边缘情况**：没有 `wx_msg_id` 的消息（系统消息等）走 fallback 哈希；media 消息两条管道编码不同（live 用 `mediaLocalPath` 原值，backfill 用相对路径）→ 同一条可能重复
- **live 启动前的消息抓不到**——历史只能靠 backfill；自由问答兜底受此影响（启动后才有 context）
- **视频**：只存路径不识别内容（caption / 视频帧理解都没接）；图片走 `worker mm` 的 OCR + 可选 vision 二轮，语音走 worker mm 的 ASR

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
- [DeepSeek](https://api.deepseek.com) — 默认示例 LLM endpoint（OpenAI 兼容）
- [DashScope / Qwen-VL](https://help.aliyun.com/zh/model-studio/) — 默认视觉模型 endpoint（OpenAI 兼容）
- [RapidOCR](https://github.com/RapidAI/RapidOCR) — 本地 PP-OCRv4 ONNX，`worker mm` 的 OCR
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 本地 ASR，`worker mm` 的语音转录
- [uv](https://docs.astral.sh/uv/) — 飞快的 Python 依赖管理

## License

[MIT](LICENSE)
