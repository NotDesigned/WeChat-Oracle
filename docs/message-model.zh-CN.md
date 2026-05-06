# 消息模型

[English](message-model.md)

这份文档是消息采集链路的数据契约，说明 WeFlow 里的微信消息如何变成标准化 SQLite 行、各种消息类型如何归一，以及 dispatcher 和 agent 最终把什么文本交给 LLM。

## 管道

```text
WeFlow live SSE event
  -> 拉取完整 /api/v1/messages row
  -> Message
  -> write_messages()
  -> SQLite

WeFlow JSON export
  -> backfill adapter
  -> Message
  -> write_messages()
  -> SQLite
```

live ingest 和 backfill 都产出同一个 Python 形状：`wechat_oracle.models.Message`，并且都通过 `wechat_oracle.ingest.writer.write_messages()` 写库。Importer 不应该直接写 SQLite。

live ingest 只把 SSE 当门铃。SSE payload 不包含足够的媒体和 app-message 元数据，所以 `ingest live` 会再从 WeFlow `/api/v1/messages` 拉完整行后归一化。Live 行使用 `source='live'`。

backfill 每次读取一个 WeFlow JSON session 导出文件。Backfill 行使用 `source='backfill'`。dispatcher 不会被 backfill 行唤醒，但搜索、总结、agent recall 和 lurk 都可以读取它们。

## 核心表

### `messages`

`messages` 是标准化时间线主表。

| 列 | 语义 |
|---|---|
| `msg_id` | 本地自增 ID。Agent 工具使用这个整数。 |
| `wx_msg_id` | WeChat MsgSvrID；WeFlow 暴露时写入。Backfill 通常有，live 可能没有。 |
| `group_id` | WeFlow session wxid，群聊通常是 `@chatroom` id。 |
| `group_name` | 从 WeFlow session metadata 捕获的显示名。 |
| `sender_wxid` | 发送者 wxid。 |
| `sender_display` | 用户可见显示名。Live 从群成员 roster 按 `groupNickname > remark > nickname > displayName` 解析，再回退到 wxid。Backfill 使用导出里的 `senderDisplayName`。 |
| `t` | Unix 秒级时间戳。 |
| `type` | 标准化消息类型，见下方类型表。 |
| `content_text` | 标准化可见文本、格式化 app 卡片预览，或媒体占位符。OCR/ASR 不写在这里。 |
| `media_path` | 相对 `WO_DATA_DIR` 的路径，位于 `media/<group_id>/<kind>/` 下。 |
| `reply_to_wx_msg_id` | 引用回复的父消息 MsgSvrID。 |
| `quote_text` | 引用回复里的被引用片段。 |
| `transcript` | 媒体 OCR/ASR 结果。`NULL` 表示未处理；`''` 表示已处理但无文字；非空文本是可用识别结果。 |
| `source` | `live` 或 `backfill`；表示入库来源，不表示处理状态。 |
| `status` | 处理状态。目前主要由 mm worker 使用，常见流转是 `raw` -> `mm_done`。 |
| `dedupe_key` | 稳定去重键。`UNIQUE(dedupe_key)` 让重复导入幂等。 |

### `forwarded_records`

合并转发 wrapper 本身存在 `messages`，`type='forward'`；展开出来的子项存在 `forwarded_records`。

| 列 | 语义 |
|---|---|
| `parent_msg_id` | forward wrapper 的本地 `messages.msg_id`。 |
| `seq` | 子项在包内的序号。 |
| `sender_display` | 原作者显示名，来自 `<sourcename>`。没有可逆 wxid。 |
| `t` | 子项原始消息时间，不是 wrapper 到达当前群的时间。 |
| `datatype` | WeChat dataitem type。`1` 是文本，其他值转成占位符。 |
| `content` | 文本子项内容，或 `[图片]`、`[语音]`、`[文件]` 之类占位符。 |
| `src_msg_id` | 原始 source message id；目前仅作信息字段。 |

## 类型归一

基础 WeChat `localType` 映射：

| WeFlow `localType` | 标准 `type` | 说明 |
|---:|---|---|
| `1` | `text` | 普通文本。 |
| `3` | `image` | 有本地文件时复制到 `data/media/...`。 |
| `34` | `voice` | 有本地文件时复制到 `data/media/...`。 |
| `43` | `video` | 只存媒体路径/占位符；暂未实现视频理解。 |
| `47` | `sticker` | 存媒体路径/占位符；本地文件可被 OCR。 |
| `48` | `text` | 位置消息；WeFlow 会把可见文字渲染到 `content`。 |
| `49` | 默认 `link` | app-message envelope，需要继续看 subtype。 |
| `10000` | `system` | 入群、退群、撤回等系统事件。 |

WeFlow 把 app-message subtype 编码为 `(appmsg.type << 32) | 49`。代码必须同时看两层：

- `base_local_type(localType)` 取低 32 位 WeChat type。
- `appmsg_subtype(localType)` 取 `<appmsg><type>`。

当前 app-message subtype 处理：

| `appmsg.type` | 标准 `type` | 入库文本 |
|---:|---|---|
| `19` | `forward` | 父消息 `content_text='[聊天记录]'`；子项解析到 `forwarded_records`。 |
| `57` | `quote` | 用户回复进 `content_text`，被引用片段进 `quote_text`，父 id 进 `reply_to_wx_msg_id`。 |
| `4`, `5` | `link` | 可解析时是 `[链接] <title>\n<url>`。 |
| `6` | `link` | `[文件] <filename>` 风格预览。 |
| `8` | `link` | `[表情/卡片] ...`。 |
| `51`, `62` | `link` | `[视频号] ...` 或卡片 fallback。 |
| `2000` | `link` | 可用时是 `[转账 <amount>] <memo>`。 |
| `2001` | `link` | 可用时是 `[红包: <blessing>]`。 |
| 其他 `49.*` | `link` | 通用 `[卡片]` 预览或 raw fallback。 |

## 媒体处理

媒体归档坚持本地优先。WeFlow 暴露本地媒体文件路径时，importer 会复制到：

```text
data/media/<group_id>/<kind>/<filename>
```

DB 只保存相对 `WO_DATA_DIR` 的路径，例如：

```text
media/12345@chatroom/images/example.jpg
```

媒体子目录：

| 类型 | 目录 |
|---|---|
| `image` | `images` |
| `voice` | `voices` |
| `video` | `videos` |
| `sticker` | `stickers` |

如果引用的媒体文件不存在，或只有远程 URL，消息仍然入库。`content_text` 会写 `[图片缺失]`、`[语音缺失]`、`[视频缺失]` 或 `[表情缺失]`。

mm worker 处理满足这些条件的行：`type IN ('image', 'voice')`、`media_path IS NOT NULL`、`transcript IS NULL`。它把 OCR/ASR 输出写入 `transcript`，并设置 `status='mm_done'`。

## Live 与 Backfill 差异

目标是给下游一个共同契约，但两个来源暴露的字段不完全一样。

| 字段 | Live | Backfill |
|---|---|---|
| `wx_msg_id` | 使用非零 `serverId`；经常缺失。 | 使用 `platformMessageId`。 |
| `sender_display` | 从 `/api/v1/group-members` 解析；回退到 `senderUsername`。 | 使用导出字段 `senderDisplayName`。 |
| `media_path` | 使用 `mediaLocalPath` 或 `mediaUrl`；只有本地文件能复制。 | 解析 JSON 文件旁边导出的相对媒体路径。 |
| quote body | 解析 appmsg subtype `57` 的 `rawContent` XML。 | 优先解析同一份 `rawContent` XML，缺失时回退到 `quotedContent` 和 `replyToMessageId` 等导出字段。 |
| forward children | 解析 `rawContent` record XML。 | 解析 `rawContent` record XML。 |

消费者不能假设两个来源的原始字段形状完全一致。dispatcher 和 agent renderer 会在 LLM 输出边界统一可见形状。

## LLM 可见渲染

当前有两套主要渲染器。

### 候选消息渲染

`dispatcher.fetch_candidates()` 支撑 `/find`、`/sum` 等 LLM 精筛命令。它返回按时间正序排列的候选消息，并使用带前缀的 ID：

- `m:<msg_id>` 表示 `messages` 行。
- `f:<forwarded_records.id>` 表示合并转发子项。

渲染规则：

| 入库行 | LLM 看到 |
|---|---|
| 普通文本 | 原始 `content_text`。 |
| 链接/卡片/文件/转账/红包 | 入库时格式化好的 `content_text`，如 `[链接] title\nurl` 或 `[红包: blessing]`。 |
| 引用回复 | `回复文本[引用 sender：被引用文本]`，live/backfill 出口形状一致。 |
| 有 transcript 的图片 | `[图片·OCR] <transcript>` |
| 有 transcript 的语音 | `[语音·ASR] <transcript>` |
| 有 transcript 的视频/表情 | `[视频·识别] <transcript>` 或 `[表情·OCR] <transcript>` |
| 无 transcript 的媒体 | `[图片]`、`[语音]`、`[视频]` 或 `[表情]` |
| 合并转发父消息 | `[聊天记录]`；当父子项都在候选窗口内时，`f:` 子项会缩进显示在父项下面。 |

中点标记（`·OCR`、`·ASR`、`·识别`）表示文本来自机器识别，不是用户手打。裸媒体占位符表示内容不可用或尚未识别。

共享的正文、媒体前缀和 quote suffix helper 位于 `src/wechat_oracle/message_render.py`。`/find`、`/sum`、agent recent context 和 agent 只读工具都使用共享 renderer 渲染 `messages` 行。合并转发子项仍直接使用 `forwarded_records.content`，因为它们已经是展开后的子消息片段，不是完整 `messages` 行。

### Agent 最近上下文

Agent chat 使用 `_fetch_recent_for_agent()` 和 `_format_recent_for_agent()` 构造初始 recent window。

每行形如：

```text
[123] 2026-05-06 12:34 [自己] Alice (wxid_xxx): message body
```

要点：

- 方括号里的整数是本地 `messages.msg_id`；agent 工具接收这个整数。
- 已知 `WO_BOT_WXID` 时，`[自己]` 标记 bot 之前说过的话。
- quote 行会追加 `[引用→m:122 image：<snippet>]` 之类后缀，让 agent 知道可以对父消息调用 `view_quoted_chain`、`read_image` 等工具。
- 媒体 transcript 标记和候选消息渲染一致：`[图片·OCR]`、`[语音·ASR]` 等。

agent 可以用工具扩展视野：

| 工具 | 用途 |
|---|---|
| `search_group_messages` | 搜索本群更早消息，支持字面 query、绝对日期范围、sender、类型过滤和前后文。会搜索 `messages.content_text`、`messages.transcript`、`messages.quote_text` 和展开后的 `forwarded_records.content`。 |
| `get_message_context` | 读取某个 `messages.msg_id` 前后的消息。 |
| `view_quoted_chain` | 沿引用回复链上溯。 |
| `expand_forward_bundle` | 展开一个合并转发 wrapper。 |
| `read_image` | 把一张图片发送给已配置的视觉模型读取。 |
| `read_voice` | 返回或即时计算一条语音消息的 ASR 文本。 |
| `read_group_memory` | 读取当前长期群记忆文档。 |

## 触发边界

dispatcher 只扫描 `source='live'` 且 `type!='system'` 的 `messages` 行。这避免历史导入唤醒 bot。

live 行被选中后再做触发分类：

- mention：文本包含 `@<WO_BOT_NAME>`。
- reply：引用回复了 bot 之前的消息。
- probability：有实质内容的 text/quote，或已有非空 transcript 的 image/voice，通过随机阈值和 cooldown。
- none：不调用 LLM。

backfill 行仍然可用于搜索、总结、agent recall 和 lurk。

## 易漂移点

修改消息模型时，需要一起检查：

- `src/wechat_oracle/schema.sql`
- `src/wechat_oracle/models.py`
- `src/wechat_oracle/ingest/writer.py`
- `src/wechat_oracle/ingest/live.py`
- `src/wechat_oracle/ingest/backfill.py`
- `src/wechat_oracle/ingest/forwarded.py`
- `src/wechat_oracle/dispatcher.py`
- `src/wechat_oracle/message_render.py`
- `src/wechat_oracle/agent/orchestrator.py`
- `src/wechat_oracle/agent/tools_read.py`
- `README.md`
- `README.zh-CN.md`

尤其是如果 LLM 可见标记发生变化，必须同时更新候选消息渲染、agent recent-context 渲染，以及解释这些标记的 prompts。
