"""Parse WeChat 合并转发 (merged-forward) appmsg payloads.

The wrapper message arrives as `localType=49` with `appmsg.type=19`. WeFlow
encodes this in a single integer: `localType = (appmsg.type << 32) | 49`, so
forwards are identifiable as `localType == FORWARD_LOCAL_TYPE` without parsing.

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
