# Message Model

[简体中文](message-model.zh-CN.md)

This document is the data contract for the message ingestion path. It describes how WeChat records from WeFlow become normalized SQLite rows, how message types are classified, and what text the dispatcher and agent expose to LLMs.

## Pipeline

```text
WeFlow live SSE event
  -> fetch full /api/v1/messages row
  -> Message
  -> write_messages()
  -> SQLite

WeFlow JSON export
  -> backfill adapter
  -> Message
  -> write_messages()
  -> SQLite
```

Both live ingest and backfill produce the same Python shape, `wechat_oracle.models.Message`, and both write through `wechat_oracle.ingest.writer.write_messages()`. Importers should not write directly to SQLite.

Live ingest uses SSE as a doorbell only. The SSE payload does not include enough metadata for media and app messages, so `ingest live` fetches full rows from WeFlow's `/api/v1/messages` endpoint before normalization. Live rows use `source='live'`.

Backfill reads one exported WeFlow JSON session at a time. Backfilled rows use `source='backfill'`. The dispatcher does not wake on backfilled rows, but search, summaries, agent recall, and lurk can read them.

## Core Tables

### `messages`

`messages` is the canonical timeline table.

| Column | Meaning |
|---|---|
| `msg_id` | Local autoincrement ID. Agent tools use this integer. |
| `wx_msg_id` | WeChat MsgSvrID when WeFlow exposes one. Backfill usually has it; live may not. |
| `group_id` | WeFlow session wxid, usually an `@chatroom` id for groups. |
| `group_name` | Display name captured from WeFlow session metadata. |
| `sender_wxid` | Sender wxid when available. |
| `sender_display` | User-visible display name. Live resolves group roster as `groupNickname > remark > nickname > displayName`, then falls back to wxid. Backfill uses WeFlow export's `senderDisplayName`. |
| `t` | Unix timestamp in seconds. |
| `type` | Normalized message type. See the type table below. |
| `content_text` | Normalized user-visible text, formatted app-card preview, or media placeholder. OCR/ASR output does not live here. |
| `media_path` | Path relative to `WO_DATA_DIR`, under `media/<group_id>/<kind>/`. |
| `reply_to_wx_msg_id` | Quoted parent MsgSvrID for quote-reply messages, when known. |
| `quote_text` | Quoted snippet for quote-reply messages. |
| `transcript` | OCR/ASR output for media. `NULL` means not processed; `''` means processed but no text; non-empty text is usable recognition output. |
| `source` | `live` or `backfill`; this records ingestion source, not processing state. |
| `status` | Processing state. Currently used mainly by the mm worker (`raw` -> `mm_done`). |
| `dedupe_key` | Stable de-duplication key. `UNIQUE(dedupe_key)` makes repeated imports idempotent. |

### `forwarded_records`

Merged-forward wrappers are stored in `messages` as `type='forward'`; their flattened child items are stored in `forwarded_records`.

| Column | Meaning |
|---|---|
| `parent_msg_id` | Local `messages.msg_id` of the forward wrapper. |
| `seq` | Child ordinal inside the bundle. |
| `sender_display` | Original author display name from `<sourcename>`. No reversible wxid is available. |
| `t` | Original child message time, not the time the wrapper arrived. |
| `datatype` | WeChat dataitem type. `1` is text; non-text values keep typed placeholders. |
| `content` | Text child content or a placeholder such as `[图片]`, `[语音]`, `[文件]`; link/file cards preserve title and URL when WeFlow exposes them. |
| `src_msg_id` | Original source message id when present; informational only. |
| `media_path` | Data-dir-relative path for child media when the record XML exposes a local file, or when `src_msg_id` matches an archived media message. |

## Type Normalization

Basic WeChat `localType` mapping:

| WeFlow `localType` | Normalized `type` | Notes |
|---:|---|---|
| `1` | `text` | Plain text. |
| `3` | `image` | Media copied into `data/media/...` when a local file is available. |
| `34` | `voice` | Media copied into `data/media/...` when a local file is available. |
| `43` | `video` | Stored as media path/placeholder; video understanding is not implemented. |
| `47` | `sticker` | Stored as media path/placeholder; OCR may process local files. |
| `48` | `text` | Location messages; WeFlow renders visible text into `content`. |
| `49` | `link` by default | App-message envelope. Refined by appmsg subtype. |
| `10000` | `system` | System events such as joins, leaves, revokes. |

WeFlow encodes app-message subtype as `(appmsg.type << 32) | 49`. Code must inspect both:

- `base_local_type(localType)` for the low 32-bit WeChat type.
- `appmsg_subtype(localType)` for `<appmsg><type>`.

Current app-message subtype handling:

| `appmsg.type` | Normalized `type` | Stored text |
|---:|---|---|
| `19` | `forward` | Parent `content_text='[聊天记录]'`; children parsed into `forwarded_records`. |
| `57` | `quote` | User reply in `content_text`, quoted snippet in `quote_text`, parent id in `reply_to_wx_msg_id`. |
| `4`, `5` | `link` | `[链接] <title>\n<url>` when parseable. |
| `6` | `link` | `[文件] <filename>` style preview. |
| `8` | `link` | `[表情/卡片] ...`. |
| `51`, `62` | `link` | `[视频号] ...` or fallback card preview. |
| `2000` | `link` | `[转账 <amount>] <memo>` when available. |
| `2001` | `link` | `[红包: <blessing>]` when available. |
| other `49.*` | `link` | Generic `[卡片]` preview or raw fallback text. |

## Media Handling

Media import is local-first. When WeFlow exposes a local media file path, importers copy it into:

```text
data/media/<group_id>/<kind>/<filename>
```

The database stores only the `WO_DATA_DIR`-relative path, such as:

```text
media/12345@chatroom/images/example.jpg
```

Media subdirectories are:

| Type | Directory |
|---|---|
| `image` | `images` |
| `voice` | `voices` |
| `video` | `videos` |
| `sticker` | `stickers` |

If a referenced media file is absent or only remote, the message is still imported. `content_text` receives a missing-media placeholder such as `[图片缺失]`, `[语音缺失]`, `[视频缺失]`, or `[表情缺失]`.

The mm worker processes rows where `type IN ('image', 'voice')`, `media_path IS NOT NULL`, and `transcript IS NULL`. It writes OCR/ASR output to `transcript` and sets `status='mm_done'`.

## Live vs Backfill Differences

The goal is a shared downstream contract, but the two sources expose different fields.

| Field | Live | Backfill |
|---|---|---|
| `wx_msg_id` | Uses `serverId` when non-zero; often absent. | Uses `platformMessageId` when available. |
| `sender_display` | Resolved from `/api/v1/group-members`; falls back to `senderUsername`. | Uses export field `senderDisplayName`. |
| `media_path` | Uses `mediaLocalPath` or `mediaUrl`; only local files can be copied. | Resolves exported relative media paths next to the JSON file. |
| quote body | Parses `rawContent` XML for appmsg subtype `57`. | Parses the same `rawContent` XML when available, falling back to export fields such as `quotedContent` and `replyToMessageId`. |
| forward children | Parses `rawContent` record XML; child media is copied when WeFlow exposes a local path, and can be linked later by `src_msg_id` if the source message is archived. | Parses `rawContent` record XML; child media is copied when the export exposes a local path, and can be linked later by `src_msg_id` if the source message is archived. |

Consumers must not assume identical raw field shapes across sources. Dispatcher and agent renderers normalize the LLM-visible shape at the output boundary.

## LLM-Visible Rendering

There are two main renderers.

### Candidate Rendering

`dispatcher.fetch_candidates()` powers `/find`, `/sum`, and related LLM-filtered commands. It returns a chronological candidate list with tagged ids:

- `m:<msg_id>` for rows from `messages`.
- `f:<forwarded_records.id>` for merged-forward children.

Rendering rules:

| Stored row | LLM-visible shape |
|---|---|
| Plain text | The original `content_text`. |
| Link/card/file/transfer/red packet | The formatted `content_text`, for example `[链接] title\nurl` or `[红包: blessing]`. |
| Quote reply | `reply text[引用 sender：quoted text]`, normalized for both live and backfill. |
| Image with transcript | `[图片·OCR] <transcript>` |
| Voice with transcript | `[语音·ASR] <transcript>` |
| Video/sticker with transcript | `[视频·识别] <transcript>` or `[表情·OCR] <transcript>` |
| Media without transcript | `[图片]`, `[语音]`, `[视频]`, or `[表情]` |
| Forward wrapper | `[聊天记录]`, with child `f:` rows rendered under the parent when both are in the candidate window. |

The middle dot markers (`·OCR`, `·ASR`, `·识别`) mean the text is machine-recognized, not user-typed. Bare media placeholders mean the content is unavailable or not yet recognized.

The shared body, media-prefix, and quote-suffix helpers live in `src/wechat_oracle/message_render.py`. `/find`, `/sum`, agent recent context, and agent read tools use the shared renderer for `messages` rows. Forwarded child rows still render from `forwarded_records.content` because they are already flattened child snippets rather than full `messages` rows.

### Agent Recent Context

Agent chat uses `_fetch_recent_for_agent()` and `_format_recent_for_agent()` to build the initial recent-message window.

Each line looks like:

```text
[123] 2026-05-06 12:34 [自己] Alice (wxid_xxx): message body
```

Important details:

- The integer in brackets is the local `messages.msg_id`; agent tools accept this integer.
- `[自己]` marks prior bot messages when `WO_BOT_WXID` is known.
- Quote rows add a suffix such as `[引用→m:122 image：<snippet>]`, so the agent can call `view_quoted_chain`, `read_image`, or other tools against the parent.
- Media transcript markers match the candidate renderer: `[图片·OCR]`, `[语音·ASR]`, and so on.

The agent can extend its view with tools:

| Tool | Purpose |
|---|---|
| `search_group_messages` | Search older messages in this group with optional substring, absolute date range, sender filter, type filter, and nearby context. Searches `messages.content_text`, `messages.transcript`, `messages.quote_text`, and flattened `forwarded_records.content`. |
| `get_message_context` | Read nearby messages before/after a specific `messages.msg_id`. |
| `view_quoted_chain` | Walk quote-reply parents up to a small depth. |
| `expand_forward_bundle` | Show children of a merged-forward wrapper. |
| `read_url` | Fetch a public HTTP(S) URL from a link/card message and return readable article text when accessible. |
| `read_image` | Send one image file to the configured vision model and return a textual reading. |
| `read_forward_child_image` | Read an image child inside a merged-forward wrapper by `parent_msg_id` + child `seq`, using `forwarded_records.media_path` or a `src_msg_id` match in the local archive. |
| `load_image` | OpenClaw MCP only: return the original image bytes as an MCP image block so the agent can inspect pixels directly. |
| `read_voice` | Return or compute ASR transcript for one voice message. |
| `read_group_memory` | Read the current long-term group memory document. |

## Trigger Boundary

The dispatcher only scans `messages` where `source='live'` and `type!='system'`. This prevents historical imports from waking the bot.

Trigger classification happens after a live row is selected:

- Mention: text contains `@<WO_BOT_NAME>`.
- Reply: row quote-replies to a prior bot message.
- Probability: eligible substantive text/quote, or image/voice with non-empty transcript, passes random threshold and cooldown.
- None: no LLM call.

Backfilled rows still participate in search, summaries, agent recall, and lurk.

## Drift Points

When changing this model, check these files together:

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

In particular, if the LLM-visible markers change, update both candidate rendering and agent recent-context rendering, plus the prompts that explain those markers.
