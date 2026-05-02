"""Reply backends for the dispatcher.

The dispatcher generates a text reply, then needs to put it back into the
WeChat conversation. Two backends:

  - `Wx4pyReplier`   — default. Drives WeChat's UI via wx4py (Windows only;
    requires main window visible). Identifies target groups by display name.
  - `StdoutReplier`  — no-op fallback. Prints to logs only. Used when
    WO_REPLY=False or wx4py fails to initialize.

(An openclaw HTTP backend was prototyped but proven incapable of group
delivery — see `openclaw.py` and the README "实验记录" section. The lower-
level `OpenclawClient` is retained for DM-only future use; the Replier
adapter was deleted as dead code.)

`build_replier()` is the single factory call from the dispatcher. It reads
`settings.reply_backend` and tries the chosen backend; on failure (wx4py
can't connect, etc.) it warns and degrades to StdoutReplier so the
dispatcher loop still runs.

Adding a backend: implement the `Replier` Protocol (just `send` and
`disconnect`) and add a branch in `build_replier()`. No dispatcher change
needed — that's the whole point of this file.
"""
from __future__ import annotations

from typing import Protocol

from loguru import logger

from .config import settings


# Four-per-em space — WeChat's @-mention separator.
_AT_SEP = " "


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
    Cooperates with our `WO_BOT_NAME` invariant: the dispatcher already
    verified at startup that wx4py's group_nickname matches WO_BOT_NAME for
    every watched group."""

    def __init__(self, wx) -> None:
        self._wx = wx

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        if not group_name:
            return
        body = f"@{requester}{_AT_SEP}{text}" if requester else text
        try:
            self._wx.chat_window.send_to(group_name, body, target_type="group")
        except Exception as e:
            logger.warning("wx4py send_to failed (group={!r}): {}", group_name, e)

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

        # Per-group identity check: warn (don't block) if logged-in account's
        # group_nickname doesn't match WO_BOT_NAME — usually means wrong account.
        for group_name in settings.groups:
            try:
                actual = wx.group_manager.get_group_nickname(group_name)
            except Exception:
                continue
            if actual and actual != settings.bot_name:
                logger.warning(
                    "wx4py: in group {!r} the logged-in account's nickname is {!r}, "
                    "but WO_BOT_NAME={!r}. Did you log into the wrong account?",
                    group_name, actual, settings.bot_name,
                )
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
