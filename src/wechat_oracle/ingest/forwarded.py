"""Parse WeChat appmsg-family (`localType` base = 49) payloads.

Despite the name, this module covers ALL `<appmsg>`-wrapped message kinds —
not just 合并转发. They share the WeFlow encoding `localType = (appmsg.type
<< 32) | 49`, the same `<msg><appmsg>...</appmsg></msg>` envelope, and the
group-message `wxid:\n` prefix.

The subtypes we currently surface (see CLAUDE.md F7/F11):
  19  → 合并转发 (RecordMsg)        → MsgType.FORWARD + forwarded_records rows
  57  → 引用回复 (quote-reply)       → MsgType.QUOTE + content/quote_text fields
  4/5 → 链接 / 文章 / 卡片 (URL card) → MsgType.LINK with `[链接] 标题\nURL`
  6   → 文件 (file)                  → MsgType.LINK with `[文件] 文件名`
  62  → 视频号短视频 (Channels)      → MsgType.LINK with `[视频号] 标题`
  51  → 视频号 feed (旧版本不支持)   → MsgType.LINK with `[微信新格式]`
  8   → 表情商店 / 微信豆            → MsgType.LINK with `[表情/卡片]`
  2000/2001 → 转账 / 红包            → MsgType.LINK with `[转账]` / `[红包]` (privacy)
  其他                               → MsgType.LINK with raw `content` fallback

The wrapper's `rawContent` looks like (in group sessions, prefixed with the
sender wxid + `:\n`):

    <?xml version="1.0"?>
    <msg>
      <appmsg>
        <title>群聊的聊天记录</title>
        <type>19</type>
        <recorditem><![CDATA[
          <recordinfo>
            <datalist count="N">
              <dataitem datatype="1" dataid="...">
                <datadesc>...text...</datadesc>
                <sourcename>...display name...</sourcename>
                <srcMsgCreateTime>...unix sec...</srcMsgCreateTime>
                <fromnewmsgid>...source-group msg id...</fromnewmsgid>
                <dataitemsource><hashusername>sha256</hashusername></dataitemsource>
              </dataitem>
              ...
            </datalist>
          </recordinfo>
        ]]></recorditem>
      </appmsg>
      <fromusername>...forwarder wxid...</fromusername>
    </msg>

Non-text dataitems (image/video/file/link/nested-forward) get a placeholder
string in `content`; we don't try to download media or recurse.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..models import ForwardedItem

# (appmsg.type << 32) | localType=49.  appmsg.type=19 = RecordMsg (合并转发).
FORWARD_LOCAL_TYPE = (19 << 32) | 49        # 81604378673
LOCAL_TYPE_BASE_MASK = 0xFFFFFFFF           # low 32 bits = real WeChat MsgType


def appmsg_subtype(local_type: int | None) -> int | None:
    """Return the encoded `<appmsg>.<type>` for a 49-base message, or None."""
    if not isinstance(local_type, int) or local_type <= 0:
        return None
    if local_type & LOCAL_TYPE_BASE_MASK != 49:
        return None
    return local_type >> 32 or None


def base_local_type(local_type: int | None) -> int | None:
    """Strip the appmsg subtype encoding from WeFlow's localType."""
    if not isinstance(local_type, int) or local_type <= 0:
        return local_type
    return local_type & LOCAL_TYPE_BASE_MASK


# Placeholder content for non-text dataitems. Mirrors the WeChat client's UI
# rendering. Unknown datatypes default to "[其他]" — better than dropping the
# row, since at least the timestamp/sender are still searchable.
_PLACEHOLDER: dict[int, str] = {
    2: "[图片]",
    3: "[语音]",
    4: "[视频]",
    5: "[链接]",
    6: "[文件]",
    8: "[表情]",
    17: "[聊天记录]",
}


# Strip the leading "wxid_xxx:\n" / "<chatroom>@chatroom_xxx:\n" that WeChat
# prepends to group-message rawContent before the actual XML body.
_GROUP_PREFIX = re.compile(r"^[^<\n]+:\s*\n(?=<\?xml|<msg)", re.DOTALL)


def parse_record_xml(raw_content: str | None) -> list[ForwardedItem]:
    """Parse `<recordinfo>` from a forwarded-records appmsg.

    Returns [] if the XML is missing/malformed or there's no `<recorditem>`.
    Items are returned in source order; `seq` matches that order.
    """
    if not raw_content:
        return []
    text = _GROUP_PREFIX.sub("", raw_content, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    recorditem = root.find(".//recorditem")
    if recorditem is None or not recorditem.text:
        return []
    try:
        info = ET.fromstring(recorditem.text)
    except ET.ParseError:
        return []

    items: list[ForwardedItem] = []
    for seq, item in enumerate(info.findall(".//dataitem")):
        dt_raw = item.get("datatype", "0")
        try:
            datatype = int(dt_raw)
        except ValueError:
            continue
        ts_raw = item.findtext("srcMsgCreateTime") or ""
        try:
            ts = int(ts_raw)
        except ValueError:
            continue

        sender = (item.findtext("sourcename") or "").strip() or None
        src_msg_id = (item.findtext("fromnewmsgid") or "").strip() or None

        if datatype == 1:
            content = (item.findtext("datadesc") or "").strip() or None
        else:
            content = _PLACEHOLDER.get(datatype, "[其他]")

        if content is None:
            # text item with no datadesc — skip; nothing useful to index
            continue

        items.append(ForwardedItem(
            seq=seq,
            sender_display=sender,
            t=ts,
            datatype=datatype,
            content=content,
            src_msg_id=src_msg_id,
        ))
    return items


# --- 49.57 quote-reply ---------------------------------------------------

# When the quoted message wasn't text, `<refermsg><content>` is the wrapped
# XML of that message — replace with a short typed placeholder. Keys are the
# values WeChat puts in `<refermsg><type>` (= WeChat MsgType, NOT localType).
_QUOTE_TYPE_PLACEHOLDER: dict[int, str] = {
    3: "[图片]",
    34: "[语音]",
    43: "[视频]",
    47: "[表情]",
    49: "[卡片消息]",
}

@dataclass(frozen=True)
class QuoteReply:
    """Parsed shape of a 49.57 引用回复.

    `content` is the user's typed reply (`<title>`); `quote_text` is the
    snippet they quoted (`<refermsg><content>`). `quote_msg_id` is the source
    message's `<svrid>` and joins back to `messages.wx_msg_id`.
    """
    content: str
    quote_text: str | None
    quote_sender: str | None
    quote_msg_id: str | None


def _strip_group_prefix(raw: str) -> str:
    return _GROUP_PREFIX.sub("", raw, count=1)


def parse_quote_reply_xml(raw_content: str | None) -> QuoteReply | None:
    """Extract reply text + refermsg from a 49.57 appmsg. None on bad XML."""
    if not raw_content:
        return None
    text = _strip_group_prefix(raw_content)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return None
    title = (appmsg.findtext("title") or "").strip()
    if not title:
        # Some quote-replies have empty title (user just sent an emoji etc.);
        # without title there's nothing to index, but we still record the
        # refermsg so dispatcher can show "X replied to Y's: ...".
        title = ""
    refer = appmsg.find("refermsg")
    quote_text = quote_sender = quote_msg_id = None
    if refer is not None:
        rd = refer.findtext("displayname")
        quote_sender = rd.strip() if rd else None
        rs = refer.findtext("svrid")
        quote_msg_id = rs.strip() if rs and rs.strip() != "0" else None
        # `<refermsg><content>` is plain text only when the quoted message was
        # text. For image / video / appmsg quotes it's the original wrapper
        # XML — useless for searching, so collapse to a typed placeholder
        # using `<refermsg><type>` (WeChat MsgType of the quoted message).
        rc = refer.findtext("content") or ""
        rt_raw = refer.findtext("type") or ""
        try:
            rt = int(rt_raw)
        except ValueError:
            rt = 0
        if rt == 1 or (rc and not rc.lstrip().startswith("<")):
            quote_text = rc.strip() or None
        elif rc:
            quote_text = _QUOTE_TYPE_PLACEHOLDER.get(rt, "[消息]")
    return QuoteReply(
        content=title,
        quote_text=quote_text,
        quote_sender=quote_sender,
        quote_msg_id=quote_msg_id,
    )


# --- other 49.* appmsg cards (link / file / video / etc.) ----------------

# Maps appmsg.subtype → human-readable bracket prefix shown to the user.
# Anything not in this dict falls through to the generic "[卡片]" prefix.
# Note: 2000/2001 (transfer/red-packet) take a custom branch in
# `format_appmsg_content` — they extract amount / blessing rather than the
# generic title/URL pair.
_CARD_LABEL: dict[int, str] = {
    4: "链接",
    5: "链接",
    6: "文件",
    8: "表情/卡片",
    51: "视频号",
    62: "视频号",
}


def format_appmsg_content(raw_content: str | None, subtype: int) -> str | None:
    """Build a one-line preview for non-forward / non-quote 49.* appmsgs.

    For most subtypes: `"[{label}] {title}\\n{url}"` (URL omitted if empty).
    Special-cases:
      2000 (转账): `"[转账 ¥99.99] {memo}"` — feedesc + pay_memo from <wcpayinfo>
      2001 (红包): `"[红包: 恭喜发财]"` — sendertitle from <wcpayinfo>

    These were previously privacy-stripped; per project decision (small
    trusted groups), amount + blessing are now indexed so the LLM can answer
    "谁给我转钱了 / 谁发的红包".

    Returns None if rawContent is missing or has no parseable `<appmsg>`.
    Caller should fall back to WeFlow's `content` field in that case.
    """
    if not raw_content:
        return None
    text = _strip_group_prefix(raw_content)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return None

    if subtype == 2000:
        fee = (appmsg.findtext(".//wcpayinfo/feedesc") or "").strip()
        memo = (appmsg.findtext(".//wcpayinfo/pay_memo") or "").strip()
        head = f"[转账 {fee}]" if fee else "[转账]"
        return f"{head} {memo}" if memo else head
    if subtype == 2001:
        blessing = (
            appmsg.findtext(".//wcpayinfo/sendertitle")
            or appmsg.findtext("title")
            or ""
        ).strip()
        return f"[红包: {blessing}]" if blessing else "[红包]"

    label = _CARD_LABEL.get(subtype, "卡片")
    title = (appmsg.findtext("title") or "").strip()
    url = (appmsg.findtext("url") or "").strip()
    parts = [f"[{label}]"]
    if title:
        parts.append(title)
    head = " ".join(parts)
    if url:
        return f"{head}\n{url}"
    return head
