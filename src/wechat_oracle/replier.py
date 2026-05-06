"""Reply backends for the dispatcher.

The dispatcher generates a text reply, then needs to put it back into the
WeChat conversation. Two backends:

  - `Wx4pyReplier`   — default. Drives WeChat's UI via wx4py (Windows only;
    requires main window visible). Identifies target groups by display name.
  - `StdoutReplier`  — no-op fallback. Prints to logs only. Used when
    WO_REPLY=False or wx4py fails to initialize.

(A Tencent iLink Bot HTTP backend was prototyped but proven incapable of
group delivery — see README "实验记录" section.)

`build_replier()` is the single factory call from the dispatcher. It reads
`settings.reply_backend` and tries the chosen backend; on failure (wx4py
can't connect, etc.) it warns and degrades to StdoutReplier so the
dispatcher loop still runs.

Adding a backend: implement the `Replier` Protocol (just `send` and
`disconnect`) and add a branch in `build_replier()`. No dispatcher change
needed — that's the whole point of this file.
"""
from __future__ import annotations

import time
from typing import Protocol

from loguru import logger

from .config import settings


# Four-per-em space — WeChat displays this after a selected @ mention.
_AT_SEP = " "


def _strip_leading_requester_mention(text: str, requester: str | None) -> str:
    """Avoid double-@ when the model starts its reply with @requester.

    `Wx4pyReplier.send` already prefixes outgoing group replies with a real
    WeChat mention. The LLM still occasionally imitates prior bot messages
    and emits "@张三 ..." itself; strip only that exact leading requester
    mention and leave all other text untouched.
    """
    if not requester:
        return text
    body = text.lstrip()
    prefix = f"@{requester}"
    if not body.startswith(prefix):
        return text
    body = body[len(prefix):]
    body = body.lstrip(" \t\r\n\u2005")
    return body


class Replier(Protocol):
    """The dispatcher only needs these two ops."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None: ...
    def disconnect(self) -> None: ...


# ---- stdout (always-available fallback) -----------------------------------


class StdoutReplier:
    """Drop messages on the floor (after they've been logged elsewhere).
    Used when WO_REPLY=False, or as a graceful degradation when the chosen
    backend can't connect."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        logger.debug("stdout-replier: would send to {}: {!r}", group_name, text[:80])

    def disconnect(self) -> None:
        pass


# ---- wx4py (current default) ----------------------------------------------


class Wx4pyReplier:
    """UI-automation backend. `_wx` is the connected wx4py.WeChatClient.

    We do NOT verify per-group nickname at startup. The previous check
    (wx4py.group_manager.get_group_nickname per group) cost 5–30s each via
    UI tab-walk and was only a soft warning. If you logged into the wrong
    WeChat account, you'll notice from the WeChat sidebar in seconds —
    cheaper than the startup tax.
    """

    def __init__(self, wx) -> None:
        self._wx = wx

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        if not group_name:
            return
        text = _strip_leading_requester_mention(text, requester)
        if requester and self._send_group_mention(group_name, requester, text):
            return

        body = f"@{requester}{_AT_SEP}{text}" if requester else text
        try:
            self._wx.chat_window.send_to(group_name, body, target_type="group")
        except Exception as e:
            logger.warning("wx4py send_to failed (group={!r}): {}", group_name, e)

    def _send_group_mention(self, group_name: str, requester: str, text: str) -> bool:
        """Send a real WeChat group @ mention.

        wx4py's public `send_to()` pastes the whole message in one shot. In
        WeChat that produces literal "@name" text, not a notification. To
        create the rich mention token, the input box must receive a typed "@",
        the member name, and a selection key before the rest of the message is
        pasted.
        """
        try:
            chat = self._wx.chat_window
            if not chat.open_chat(group_name, target_type="group"):
                return False
            edit = chat._get_chat_input()
            edit = chat.prepare_input_for_paste(edit)
            if not edit:
                return False

            before_candidates = self._mention_candidate_signatures(chat.root, requester)
            edit.SendKeys("@")
            time.sleep(0.2)
            if not chat.paste_text_into_focused_input(
                requester, log_error="写入 @ 对象到剪贴板失败"
            ):
                return False
            time.sleep(0.5)
            after_candidates = self._mention_candidate_signatures(chat.root, requester)
            if not (after_candidates - before_candidates):
                logger.warning(
                    "wx4py @ candidate not visible for requester={!r}", requester
                )
                try:
                    edit.SendKeys("{Ctrl}a")
                    edit.SendKeys("{Delete}")
                except Exception:
                    pass
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.2)

            suffix = text.strip()
            if suffix:
                if not chat.paste_text_into_focused_input(
                    _AT_SEP + suffix,
                    log_error="写入 @ 回复正文到剪贴板失败",
                ):
                    return False
            edit.SendKeys("{Enter}")
            time.sleep(0.3)
            logger.info("wx4py sent real @ mention to requester={!r}", requester)
            return True
        except Exception as e:
            logger.warning(
                "wx4py real @ mention failed (group={!r}, requester={!r}); "
                "falling back to plain text: {}",
                group_name, requester, e,
            )
            return False

    def _mention_candidate_signatures(self, root, requester: str) -> set[tuple[str, str, int, int]]:
        """Best-effort snapshot of visible controls that look like @ candidates.

        Without this guard, pressing Enter after a failed @ lookup can send the
        bare "@name" text. We take a before/after snapshot and require a new
        matching non-edit control to appear, so existing chat messages that
        happen to contain the requester's name do not count.
        """
        target = requester.strip()
        if not target:
            return set()
        found: set[tuple[str, str, int, int]] = set()

        def walk(ctrl, depth: int) -> None:
            if depth > 8 or ctrl is None:
                return
            try:
                name = (ctrl.Name or "").strip()
                control_type = ctrl.ControlTypeName or ""
                if target in name and control_type != "EditControl":
                    rect = ctrl.BoundingRectangle
                    top = int(getattr(rect, "top", 0) or 0)
                    left = int(getattr(rect, "left", 0) or 0)
                    found.add((name, control_type, left, top))
            except Exception:
                pass
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return found

    def disconnect(self) -> None:
        try:
            self._wx.disconnect()
        except Exception as e:
            logger.warning("wx4py disconnect failed: {}", e)

    @classmethod
    def try_connect(cls) -> Replier | None:
        """Returns a connected Wx4pyReplier or None if wx4py is unhappy.
        Caller decides whether to fall back to stdout."""
        try:
            from wx4py import WeChatClient
        except ImportError:
            logger.warning("wx4py not installed; can't use wx4py backend")
            return None
        try:
            wx = WeChatClient()
            wx.connect()
        except Exception as e:
            logger.warning(
                "wx4py connect failed ({}); replies disabled this run. "
                "Open WeChat's main window (not in tray) and restart dispatcher.", e,
            )
            return None
        return cls(wx)


# ---- factory --------------------------------------------------------------


def build_replier() -> Replier:
    """Build the configured replier. Always returns a working Replier (may
    be StdoutReplier if backend init failed)."""
    if not settings.reply:
        logger.info("WO_REPLY=False; using stdout replier")
        return StdoutReplier()

    backend = (settings.reply_backend or "wx4py").lower()
    if backend == "stdout":
        return StdoutReplier()
    if backend == "wx4py":
        replier = Wx4pyReplier.try_connect()
        return replier or StdoutReplier()

    logger.warning(
        "unknown WO_REPLY_BACKEND={!r}; valid: wx4py / stdout. Using stdout.", backend,
    )
    return StdoutReplier()
