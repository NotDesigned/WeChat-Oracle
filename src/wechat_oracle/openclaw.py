"""OpenClaw / iLink Bot HTTP client (experimental).

Tencent's official WeChat bot API at https://ilinkai.weixin.qq.com. Reverse-
followed from upstream demo at hao-ji-xing/openclaw-weixin (MJS bridge +
TypeScript SDK). Pure HTTP/JSON, no Windows/UI dependency — runs anywhere
with HTTPS.

What's CONFIRMED working (per upstream demo):
  - QR-based login → bot_token + ilink_bot_id, persisted to disk
  - Long-poll getupdates (35s server hold) for inbound 1-on-1 messages
  - sendmessage with `to_user_id` for direct replies
  - Session timeout detection (`-14`) → re-login required

What's UNCONFIRMED (the experiment we're about to run):
  - Whether `group_id` field of WeixinMessage is actually populated for group
    messages — protocol layer (api/types.ts) defines it; client layer
    (messaging/inbound.ts) hardcodes ChatType: "direct"
  - Whether sendmessage with group_id (or to_user_id pointing at a group)
    actually delivers
  - What namespace group sessions use (vs. @im.wechat / @im.bot)

Use the CLI commands (`wechat-oracle openclaw login|probe|send`) to figure
this out before wiring into the dispatcher.

Token is stored at `<data_dir>/openclaw-token.json` with mode 0600 (best
effort on Windows). Can override with WO_OPENCLAW_TOKEN_PATH.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"  # hardcoded upstream; semantics unknown
CHANNEL_VERSION = "1.0.2"

# Long-poll: server holds up to 35s; client gives 38s before tripping.
GETUPDATES_TIMEOUT_S = 38.0
# Status polls can be slow (Tencent → CN routing). Bridge.mjs has no explicit
# GET timeout (Node fetch default ~30s); match that.
DEFAULT_TIMEOUT_S = 30.0


@dataclass
class OpenclawSession:
    """Persisted login state. Loaded from / saved to JSON."""
    token: str
    base_url: str
    bot_id: str          # ilink_bot_id; appears as `from_user_id` in our outgoing msgs
    user_id: str         # ilink_user_id; the human owner of the bot
    saved_at: str        # ISO8601 UTC

    @classmethod
    def from_json(cls, path: Path) -> OpenclawSession | None:
        if not path.exists():
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            token=d["token"],
            base_url=d.get("baseUrl", DEFAULT_BASE_URL),
            bot_id=d.get("accountId") or d.get("bot_id", ""),
            user_id=d.get("userId") or d.get("user_id", ""),
            saved_at=d.get("savedAt") or d.get("saved_at", ""),
        )

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        d = {
            "token": self.token,
            "baseUrl": self.base_url,
            "accountId": self.bot_id,
            "userId": self.user_id,
            "savedAt": self.saved_at,
        }
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        # Best-effort restrict permissions; on Windows this is mostly no-op.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _random_wechat_uin() -> str:
    """Reproduce upstream's X-WECHAT-UIN: random uint32 → decimal str → base64."""
    val = secrets.randbits(32)
    return base64.b64encode(str(val).encode("utf-8")).decode("ascii")


def _build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class OpenclawClient:
    """Thin httpx wrapper. One client = one bot session."""

    def __init__(self, session: OpenclawSession | None = None, base_url: str = DEFAULT_BASE_URL):
        self.session = session
        self.base_url = (session.base_url if session else base_url).rstrip("/")
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=GETUPDATES_TIMEOUT_S, write=10.0, pool=10.0),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenclawClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def token(self) -> str | None:
        return self.session.token if self.session else None

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        r = self._http.get(url, timeout=DEFAULT_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any] | None:
        """POST returns parsed JSON or None on long-poll timeout (which is fine)."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        payload = {**body, "base_info": {"channel_version": CHANNEL_VERSION}}
        try:
            r = self._http.post(
                url,
                headers=_build_headers(self.token),
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
        except httpx.ReadTimeout:
            return None  # long-poll timeout
        r.raise_for_status()
        return r.json()

    # ---- login ---------------------------------------------------------

    def request_qr(self) -> tuple[str, str]:
        """Fetch a fresh login QR. Returns (qrcode, qrcode_img_content). The
        latter is a URL to encode as a QR image (NOT a server-rendered PNG)."""
        d = self._get(f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
        return d["qrcode"], d["qrcode_img_content"]

    def poll_qr_status(self, qrcode: str) -> dict[str, Any]:
        """One-shot status poll. Status values observed: wait / scaned /
        expired / confirmed. On confirmed, response includes `bot_token`,
        `baseurl`, `ilink_bot_id`, `ilink_user_id`.

        ⚠️ This is actually a long-poll: the server holds the connection
        for ~30s on `wait` before returning. Use a generous timeout (>30s).
        """
        from urllib.parse import quote
        url = f"{self.base_url}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}"
        # 60s = 30s server-hold + ample headroom for round-trip latency.
        r = self._http.get(url, timeout=60.0)
        r.raise_for_status()
        return r.json()

    # ---- messaging -----------------------------------------------------

    def get_updates(self, buf: str = "") -> dict[str, Any]:
        """Long-poll. Returns dict with `msgs` list and `get_updates_buf`
        (cursor to pass next call). Empty {ret:0, msgs:[]} on timeout.

        Note on buf: bridge.mjs uses `?? buf` (truthy-only update); we mirror
        that — caller should do `buf = resp.get("get_updates_buf") or buf`.
        Don't use `dict.get(k, default)` for this: if server returns the key
        with a null value, that overwrites your real buf with None.
        """
        resp = self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": buf},
            timeout=GETUPDATES_TIMEOUT_S,
        )
        return resp or {"ret": 0, "msgs": [], "get_updates_buf": buf}

    def send_text(
        self,
        *,
        to_user_id: str | None = None,
        group_id: str | None = None,
        text: str,
        context_token: str | None = None,
    ) -> str:
        """Send a text message. EXPERIMENTAL on group_id.

        Upstream demo only ever passes `to_user_id`. The proto type
        (`api/types.ts WeixinMessage`) admits a `group_id?: string` field
        but the client SDK doesn't use it. We're trying both routes here:

        - to_user_id only → confirmed working for 1-on-1
        - group_id only or both → unknown; experiment will tell

        Returns the client_id we sent (useful for echo dedup if we ever
        observe our own outgoing messages in getupdates).
        """
        if not (to_user_id or group_id):
            raise ValueError("need to_user_id or group_id")
        client_id = f"wo-{uuid.uuid4()}"
        msg: dict[str, Any] = {
            "from_user_id": "",  # server fills from token
            "client_id": client_id,
            "message_type": 2,   # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if to_user_id is not None:
            msg["to_user_id"] = to_user_id
        if group_id is not None:
            msg["group_id"] = group_id
        if context_token is not None:
            msg["context_token"] = context_token
        self._post("ilink/bot/sendmessage", {"msg": msg})
        return client_id


# --- helpers --------------------------------------------------------------


def extract_text_from_msg(msg: dict[str, Any]) -> str:
    """Pull a human-readable string out of a WeixinMessage's item_list.
    Mirrors upstream demo's switch on item.type."""
    for item in msg.get("item_list", []) or []:
        t = item.get("type")
        if t == 1 and item.get("text_item", {}).get("text"):
            return item["text_item"]["text"]
        if t == 3 and item.get("voice_item", {}).get("text"):
            return f"[语音] {item['voice_item']['text']}"
        if t == 2:
            return "[图片]"
        if t == 4:
            return f"[文件] {item.get('file_item', {}).get('file_name', '')}"
        if t == 5:
            return "[视频]"
    return "[空消息]"


def login_interactive(
    client: OpenclawClient,
    *,
    on_qr: callable,
    poll_interval_s: float = 1.0,
    timeout_s: float = 300.0,
) -> OpenclawSession:
    """Run the full QR login dance. Calls `on_qr(qr_url)` so the caller can
    render however they want (terminal QR, save PNG, etc.). Polls status
    until 'confirmed' or timeout, refreshing the QR up to 3 times on
    'expired'.
    """
    qrcode, qr_url = client.request_qr()
    on_qr(qr_url)
    refreshes = 0
    transient_errors = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = client.poll_qr_status(qrcode)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            # Tencent's CN routing is occasionally slow; eat a few transient
            # failures rather than aborting login.
            transient_errors += 1
            if transient_errors > 10:
                raise RuntimeError(f"too many poll errors: {e}") from e
            logger.warning("poll #{} failed ({}); retrying", transient_errors, e)
            time.sleep(poll_interval_s)
            continue
        status = resp.get("status")
        if status == "wait":
            pass
        elif status == "scaned":
            logger.info("scanned, waiting for confirmation in WeChat...")
        elif status == "expired":
            refreshes += 1
            if refreshes > 3:
                raise RuntimeError("QR expired 3 times; aborting login")
            logger.info("QR expired, refreshing ({}/3)", refreshes)
            qrcode, qr_url = client.request_qr()
            on_qr(qr_url)
        elif status == "confirmed":
            from datetime import datetime, timezone
            session = OpenclawSession(
                token=resp["bot_token"],
                base_url=resp.get("baseurl") or DEFAULT_BASE_URL,
                bot_id=resp.get("ilink_bot_id", ""),
                user_id=resp.get("ilink_user_id", ""),
                saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            client.session = session
            client.base_url = session.base_url.rstrip("/")
            return session
        time.sleep(poll_interval_s)
    raise RuntimeError("login timed out")


def render_qr_to_terminal(url: str) -> None:
    """Render the QR right in the terminal as ASCII. Uses qrcode's print_ascii
    which writes Unicode block chars (▀▄█) — fine on any UTF-8 terminal, but
    Windows' default cp936/GBK chokes. We force stdout to UTF-8 first; if that
    fails too we fall back to a pure-ASCII '## ' renderer (chunkier but works
    anywhere). No PNG, no temp files.
    """
    try:
        import qrcode  # type: ignore[import-not-found]
    except ImportError:
        print("\n[qrcode not installed; paste this URL into any QR generator:]")
        print(f"\n  {url}\n", flush=True)
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)

    # Try the nice Unicode block render. Reconfigure stdout to UTF-8 first
    # (Python 3.7+); harmless on POSIX where it's already UTF-8.
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print()  # blank line before QR
    try:
        qr.print_ascii(out=_sys.stdout, invert=True)
        _sys.stdout.flush()
        return
    except (UnicodeEncodeError, OSError):
        pass

    # Fallback: pure-ASCII renderer using '##' (black) and '  ' (white).
    # Chunkier but bulletproof — no encoding issues on any terminal.
    matrix = qr.get_matrix()
    for row in matrix:
        line = "".join("##" if cell else "  " for cell in row)
        print(line, flush=True)
    print(flush=True)
