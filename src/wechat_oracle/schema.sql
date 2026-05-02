-- WeChat-Oracle SQLite schema (v1)
-- Single source of truth for normalized chat messages.
-- Status field drives the downstream pipeline (mm -> segmenter -> indexer).

CREATE TABLE IF NOT EXISTS messages (
    msg_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wx_msg_id           TEXT,           -- WeChat MsgSvrID; present for backfill, often null for live
    group_id            TEXT NOT NULL,  -- group's wxid (backfill) or display-name fallback (live)
    group_name          TEXT,
    sender_wxid         TEXT,
    sender_display      TEXT,           -- 群昵称 / 备注 / 微信昵称, in that priority
    t                   INTEGER NOT NULL,  -- unix seconds, UTC
    type                TEXT NOT NULL,     -- text|image|voice|video|link|forward|quote|sticker|system
    content_text        TEXT,           -- raw text; for media filled by mm worker via OCR/ASR
    media_path          TEXT,           -- relative path under media/ (content-addressed)
    reply_to_wx_msg_id  TEXT,           -- parent's wx_msg_id when this is a quote/reply
    quote_text          TEXT,           -- snippet of quoted msg, when wxauto can't resolve parent id
    transcript          TEXT,           -- OCR/ASR output for media (image/voice). NULL = not yet processed; '' = processed, no text found
    source              TEXT NOT NULL CHECK (source IN ('live', 'backfill')),
    status              TEXT NOT NULL DEFAULT 'raw'
                        CHECK (status IN ('raw', 'mm_pending', 'mm_done', 'assigned', 'indexed')),
    dedupe_key          TEXT NOT NULL,  -- per-source-determined; used to avoid duplicate inserts
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_messages_group_t       ON messages (group_id, t);
CREATE INDEX IF NOT EXISTS idx_messages_status        ON messages (status);
CREATE INDEX IF NOT EXISTS idx_messages_sender        ON messages (sender_wxid);
CREATE INDEX IF NOT EXISTS idx_messages_wx_msg_id     ON messages (wx_msg_id);

-- Lightweight per-group state: cursor for incremental backfill, last-seen for live polling.
CREATE TABLE IF NOT EXISTS group_state (
    group_id            TEXT PRIMARY KEY,
    group_name          TEXT,
    last_backfill_t     INTEGER,        -- max(t) successfully imported via backfill
    last_live_t         INTEGER,        -- max(t) seen by live poller
    last_live_dedupe    TEXT
);

-- Children of WeChat 合并转发 (merged-forward) messages. The wrapper itself lives
-- in `messages` as type='forward'; this table holds each `<dataitem>` from its
-- `<recordinfo>` XML, flattened one level (nested forwards are placeholder only).
-- sender_display comes from `<sourcename>` (no wxid available; `<hashusername>`
-- is sha256 and not invertible). timestamp is `<srcMsgCreateTime>` of the
-- original message in its source group, NOT the time of the forward.
CREATE TABLE IF NOT EXISTS forwarded_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_msg_id   INTEGER NOT NULL REFERENCES messages(msg_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,    -- 0-based ordinal within the bundle
    sender_display  TEXT,                -- sourcename (display name; no wxid)
    t               INTEGER NOT NULL,    -- srcMsgCreateTime (Unix sec)
    datatype        INTEGER NOT NULL,    -- 1=text; others get placeholder content
    content         TEXT,                -- html-unescaped datadesc, or "[图片]" etc.
    src_msg_id      TEXT,                -- fromnewmsgid (informational; not joined)
    UNIQUE(parent_msg_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_fwd_records_parent ON forwarded_records(parent_msg_id);
CREATE INDEX IF NOT EXISTS idx_fwd_records_t      ON forwarded_records(t);
CREATE INDEX IF NOT EXISTS idx_fwd_records_sender ON forwarded_records(sender_display);

-- Tracks dispatcher runs: which incoming command messages have been processed.
-- Decoupled from `messages.status` so the message lifecycle stays clean.
CREATE TABLE IF NOT EXISTS command_runs (
    msg_id      INTEGER PRIMARY KEY,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
    result      TEXT
);

-- Schema version, for future migrations. Bumped manually when DDL changes.
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '1');
