"""B站互动轮询：评论回复通知 / @通知 / 动态评论区 / 私信回复。

只做「收到消息 -> 生成回复 -> 用登录身份发出」，不点赞不刷量。
接口全部走 Web 端 cookie(SESSDATA/bili_jct)，不依赖 WebSocket。

约束：
- 状态持久化到 interact_state.json，重启不重复回
- 首次轮询只记水位，不补历史（避免一上线刷一堆）
- 每条通道都有每日上限 + 单轮上限
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NOTIFY_REPLY = "https://api.bilibili.com/x/msgfeed/reply"
_NOTIFY_AT = "https://api.bilibili.com/x/msgfeed/at"
_DYNAMIC_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
_REPLY_LIST = "https://api.bilibili.com/x/v2/reply"
_REPLY_ADD = "https://api.bilibili.com/x/v2/reply/add"
_SESSIONS = "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
_SESSION_MSGS = "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
_SEND_MSG = "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg"


def _hdr(referer: str = "https://www.bilibili.com/") -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
    }


async def _get(url, cookies, params=None, referer="https://www.bilibili.com/", timeout=15):
    import aiohttp

    async with aiohttp.ClientSession(
        cookies=cookies, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as s:
        async with s.get(url, params=params, headers=_hdr(referer)) as r:
            txt = await r.text()
    try:
        return json.loads(txt)
    except Exception:
        return {"code": -1, "message": txt[:200]}


async def _post(url, cookies, data, referer="https://www.bilibili.com/", timeout=20):
    import aiohttp

    async with aiohttp.ClientSession(
        cookies=cookies, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as s:
        async with s.post(url, data=data, headers=_hdr(referer)) as r:
            txt = await r.text()
    try:
        return json.loads(txt)
    except Exception:
        return {"code": -1, "message": txt[:200]}


def _strip_at(text: str) -> str:
    """去掉 @xxx 前缀。"""
    t = str(text or "").strip()
    while t.startswith("@"):
        parts = t.split(maxsplit=1)
        if len(parts) == 2:
            t = parts[1].strip()
        else:
            t = t.lstrip("@").strip()
            break
    return t


class InteractState:
    """持久化已处理消息 id 与发信记录。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.setdefault("seen", [])
                    d.setdefault("initialized", False)
                    d.setdefault("sent", [])
                    d.setdefault("day", "")
                    d.setdefault("today_count", 0)
                    d.setdefault("dm_cursor", 0)
                    d.setdefault("device_id", "")
                    return d
        except Exception as e:
            logger.warning(f"[bili_watcher] 读互动状态失败: {e}")
        return {
            "seen": [],
            "initialized": False,
            "sent": [],
            "day": "",
            "today_count": 0,
            "dm_cursor": 0,
            "device_id": "",
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[bili_watcher] 存互动状态失败: {e}")

    def seen(self) -> list[str]:
        return self.data["seen"]

    def mark(self, key: str) -> None:
        s = self.data["seen"]
        s.append(str(key))
        if len(s) > 500:
            del s[:-500]

    def inited(self) -> bool:
        return bool(self.data.get("initialized"))

    def mark_inited(self) -> None:
        self.data["initialized"] = True
        self.save()

    def roll_day(self) -> None:
        import datetime as _dt

        today = _dt.date.today().isoformat()
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["today_count"] = 0

    def add_sent(self, text: str) -> None:
        self.roll_day()
        self.data["today_count"] = int(self.data.get("today_count", 0)) + 1
        self.data["sent"].append(
            {"time": int(time.time()), "text": str(text)[:120]}
        )
        if len(self.data["sent"]) > 200:
            del self.data["sent"][:-200]
        self.save()

    def today_count(self) -> int:
        self.roll_day()
        return int(self.data.get("today_count", 0))


async def fetch_reply_notifs(cookies: dict, limit: int = 10) -> list[dict]:
    """拉评论回复通知（含回复通知）。"""
    d = await _get(_NOTIFY_REPLY, cookies, params={"ps": limit, "pn": 1})
    if d.get("code") != 0:
        logger.debug(f"[bili_watcher] 回复通知: {d.get('code')} {d.get('message')}")
        return []
    out = []
    for item in (d.get("data") or {}).get("items") or []:
        r = item.get("item") or {}
        u = item.get("user") or {}
        rpid = str(r.get("source_id") or "")
        if not rpid:
            continue
        out.append(
            {
                "key": f"reply:{rpid}",
                "channel": "comment",
                "rpid": rpid,
                "mid": str(u.get("mid") or ""),
                "username": str(u.get("nickname") or "") or str(u.get("mid") or ""),
                "content": str(r.get("source_content") or "").strip(),
                "oid": int(r.get("subject_id") or 0),
                "biz": int(r.get("business_id") or 1),
                "thread_id": str(r.get("root_id") or rpid),
            }
        )
    return out


async def fetch_at_notifs(cookies: dict, limit: int = 10) -> list[dict]:
    """拉 @ 通知。"""
    d = await _get(_NOTIFY_AT, cookies, params={"ps": limit, "pn": 1})
    if d.get("code") != 0:
        logger.debug(f"[bili_watcher] @通知: {d.get('code')} {d.get('message')}")
        return []
    out = []
    for item in (d.get("data") or {}).get("items") or []:
        at_id = str(item.get("id") or "")
        source = item.get("item") or {}
        u = item.get("user") or {}
        rpid = str(source.get("source_id") or "")
        content = _strip_at(source.get("source_content") or "")
        if not content:
            continue
        out.append(
            {
                "key": f"at:{at_id or rpid}",
                "channel": "comment",
                "rpid": rpid,
                "at_id": at_id,
                "mid": str(u.get("mid") or ""),
                "username": str(u.get("nickname") or "") or str(u.get("mid") or ""),
                "content": content,
                "oid": int(source.get("subject_id") or 0),
                "biz": int(source.get("business_id") or 1),
                "thread_id": str(source.get("root_id") or rpid or at_id),
            }
        )
    return out


async def fetch_dynamic_comment_notifs(
    cookies: dict, my_mid: str, limit: int = 10
) -> list[dict]:
    """拉自己动态的最新评论。"""
    if not my_mid:
        return []
    d = await _get(
        _DYNAMIC_FEED,
        cookies,
        params={"timezone_offset": -480, "platform": "web", "features": "itemOpusStyle", "mid": my_mid},
        referer=f"https://space.bilibili.com/{my_mid}/dynamic",
    )
    if d.get("code") != 0:
        logger.debug(f"[bili_watcher] 动态列表: {d.get('code')} {d.get('message')}")
        return []

    out: list[dict] = []
    items = (d.get("data") or {}).get("items") or []
    for it in items[:3]:
        dyn_id = str(it.get("id_str") or "")
        if not dyn_id or not dyn_id.isdigit():
            continue
        rd = await _get(
            _REPLY_LIST,
            cookies,
            params={"type": 17, "oid": dyn_id, "sort": 2, "ps": limit, "pn": 1},
            referer=f"https://t.bilibili.com/{dyn_id}",
        )
        if rd.get("code") != 0:
            continue
        for r in (rd.get("data") or {}).get("replies") or []:
            rid = str(r.get("rpid") or "")
            if not rid:
                continue
            out.append(
                {
                    "key": f"dyn:{rid}",
                    "channel": "comment",
                    "rpid": rid,
                    "mid": str((r.get("member") or {}).get("mid") or ""),
                    "username": str((r.get("member") or {}).get("uname") or ""),
                    "content": str((r.get("content") or {}).get("message") or "").strip(),
                    "oid": int(r.get("oid") or dyn_id),
                    "biz": 17,
                    "thread_id": str(r.get("root") and (r.get("root") or {}).get("rpid") or rid),
                    "root": r.get("root") or None,
                }
            )
        if len(out) >= limit:
            break
    return out


async def fetch_private_inbox(
    cookies: dict, my_mid: str, device_id: str, cursor: int
) -> tuple[int, list[dict]]:
    """拉所有私聊会话里发给我且晚于 cursor 的未读私信。

    返回 (新cursor, 消息列表)。
    """
    if not my_mid:
        return cursor, []
    h_extra = {
        "Referer": "https://message.bilibili.com/",
        "Origin": "https://message.bilibili.com",
    }
    data = await _get(
        _SESSIONS,
        cookies,
        params={
            "session_type": 1,
            "group_fold": 1,
            "unfollow_fold": 0,
            "sort_rule": 2,
            "size": 100,
            "build": 0,
            "mobi_app": "web",
        },
        referer="https://message.bilibili.com/",
    )
    if data.get("code") != 0:
        logger.debug(f"[bili_watcher] 私信会话: {data.get('code')} {data.get('message')}")
        return cursor, []
    new_cursor = cursor
    msgs: list[dict] = []
    for sess in (data.get("data") or {}).get("session_list") or []:
        talker = sess.get("session", {})
        talker_id = str(talker.get("talker_id") or "")
        session_type = int(talker.get("session_type") or 1)
        if not talker_id or talker_id == my_mid:
            continue
        name = str(talker.get("name") or talker_id)
        md = await _get(
            _SESSION_MSGS,
            cookies,
            params={
                "talker_id": talker_id,
                "session_type": session_type,
                "begin_seqno": cursor,
                "size": 10,
                "sender_device_id": 1,
                "build": 0,
                "mobi_app": "web",
            },
            referer="https://message.bilibili.com/",
        )
        if md.get("code") != 0:
            continue
        detail = md.get("data") or {}
        if detail.get("has_new"):
            new_cursor = max(new_cursor, int(detail.get("last_seqno") or cursor))
        for m in detail.get("msgs") or []:
            mid_sender = str(m.get("msg", {}).get("sender_id") or "")
            if mid_sender != talker_id:
                continue  # 只要对方发来的
            raw = m.get("msg", {}).get("content", {})
            text = ""
            mtype = int(m.get("msg", {}).get("msg_type") or 1)
            if isinstance(raw, dict):
                text = str(raw.get("content") or raw.get("text") or "")
            elif isinstance(raw, str):
                try:
                    j = json.loads(raw)
                    text = str(j.get("content") or j.get("text") or "") if isinstance(j, dict) else raw
                except Exception:
                    text = raw
            text = str(text or "").strip()
            if not text:
                continue
            msgs.append(
                {
                    "key": f"dm:{m.get('msg', {}).get('msg_id', '')}",
                    "channel": "dm",
                    "mid": talker_id,
                    "username": name,
                    "content": text,
                }
            )
    return new_cursor, msgs


async def send_private_msg(
    cookies: dict, my_mid: str, target_mid: str, text: str, device_id: str = ""
) -> bool:
    """给指定 B站用户发一条私信（纯文本）。"""
    if not device_id:
        device_id = str(uuid.uuid4()).upper()

    data = {
        "msg[sender_uid]": my_mid,
        "msg[receiver_id]": str(target_mid),
        "msg[receiver_type]": 1,
        "msg[msg_type]": 1,
        "msg[msg_status]": 0,
        "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
        "msg[dev_id]": device_id,
        "msg[new_face_version]": "0",
        "msg[timestamp]": int(time.time()),
        "from_filework": 0,
        "build": 0,
        "mobi_app": "web",
    }
    d = await _post(_SEND_MSG, cookies, data, referer="https://message.bilibili.com/")
    ok = d.get("code") == 0
    if not ok:
        logger.warning(f"[bili_watcher] 发私信失败: {d.get('code')} {d.get('message')}")
    return ok


async def send_comment_reply(
    cookies: dict, oid: int, biz: int, text: str, root: int | None = None
) -> bool:
    """用登录身份发一条评论/回复。"""
    rtype = _BIZ_TYPE.get(biz, 1)
    data = {
        "oid": str(oid),
        "type": str(rtype),
        "message": text,
        "plat": 1,
    }
    if root:
        data["root"] = str(root)
        data["parent"] = str(root)
    d = await _post(_REPLY_ADD, cookies, data)
    ok = d.get("code") == 0
    if not ok:
        logger.warning(f"[bili_watcher] 发评论失败: {d.get('code')} {d.get('message')}")
    return ok


async def get_my_mid(cookies: dict) -> str:
    """取登录账号的 UID。"""
    d = await _get("https://api.bilibili.com/x/web-interface/nav", cookies)
    if d.get("code") == 0:
        return str((d.get("data") or {}).get("mid") or "")
    return ""


# ================= 主轮询 =================

_REPLY_SYS = (
    "你在 B 站，有人跟你的账号互动了。你要用这个账号的身份回一句。"
    "要求：{style}\n"
    "只输出回复本身，不要引号、不要解释、不要'感谢'开头这种客套。"
    "公开场合礼貌自然，敏感话题不站队、不表态。"
)


def _build_interact_prompt(item: dict) -> str:
    ch = item.get("channel")
    username = item.get("username", "")
    content = item.get("content", "")[:500]
    if ch == "dm":
        scene = "这是 B 站私信，一对一聊天。"
    else:
        if item.get("at_id"):
            scene = "这是 B 站评论区，有人 @了你。"
        elif item.get("biz") == 17:
            scene = "这是你发的动态下面的评论。"
        else:
            scene = "这是 B 站评论区，有人回复了你的评论。"
    return (
        f"{scene}\n"
        f"对方：{username}\n"
        f"对方内容：{content}\n"
        "请回一句。"
    )


async def poll_once(
    state: InteractState,
    cookies: dict,
    my_mid: str,
    gen_reply,
    style: str = "",
    daily_limit: int = 20,
    per_poll_limit: int = 3,
    dm_enabled: bool = False,
    dm_cursor: int = 0,
    device_id: str = "",
    dm_whitelist: list[str] | None = None,
) -> dict:
    """跑一轮互动。gen_reply 是 async (item)->str 的回复生成函数。

    返回 {"replied": int, "replies": [ {username, channel, text} ], "dm_cursor": int}
    """
    replied = 0
    replies = []
    seen = set(state.seen())

    def _can_add(key: str, channel: str) -> bool:
        if state.today_count() >= daily_limit:
            return False
        if replied >= per_poll_limit:
            return False
        return key not in seen

    cands: list[dict] = []
    try:
        cands += await fetch_reply_notifs(cookies)
    except Exception as e:
        logger.warning(f"[bili_watcher] 拉回复通知异常: {e}")
    try:
        cands += await fetch_at_notifs(cookies)
    except Exception as e:
        logger.warning(f"[bili_watcher] 拉@通知异常: {e}")
    try:
        cands += await fetch_dynamic_comment_notifs(cookies, my_mid)
    except Exception as e:
        logger.warning(f"[bili_watcher] 拉动态评论异常: {e}")

    if not state.inited():
        # 首次只记水位
        for c in cands:
            state.mark(c["key"])
        # 私信 cursor 也先取一次
        if dm_enabled:
            try:
                dm_cursor, _ = await fetch_private_inbox(cookies, my_mid, device_id, dm_cursor)
            except Exception:
                pass
        state.mark_inited()
        logger.info(f"[bili_watcher] 互动首次初始化，已记 {len(cands)} 条水位")
        return {"replied": 0, "replies": [], "dm_cursor": dm_cursor}

    for c in cands:
        if replied >= per_poll_limit:
            break
        if not _can_add(c["key"], c["channel"]):
            continue
        content = c.get("content") or ""
        if len(content.strip()) < 1:
            state.mark(c["key"])
            continue
        # 广告/纯符号跳过
        compact = re.sub(r"\W+", "", content, flags=re.UNICODE)
        if not compact:
            state.mark(c["key"])
            continue
        text = ""
        try:
            text = await gen_reply(c)
        except Exception as e:
            logger.warning(f"[bili_watcher] 生成回复异常: {e}")
        text = str(text or "").strip().strip('"\'“”‘’')
        if not text or len(text) > 120:
            state.mark(c["key"])
            continue
        ok = False
        try:
            root = int(c.get("thread_id") or 0) if c.get("channel") == "comment" else None
            oid = int(c.get("oid") or 0)
            ok = await send_comment_reply(
                cookies, oid, int(c.get("biz") or 1), text,
                root=None if not root or root == int(c.get("rpid") or 0) else root,
            )
        except Exception as e:
            logger.warning(f"[bili_watcher] 发评论异常: {e}")
        if ok:
            state.mark(c["key"])
            state.add_sent(text)
            replied += 1
            replies.append({"username": c.get("username", ""), "channel": c.get("channel"), "text": text})
            logger.info(f"[bili_watcher] 已回复({c.get('channel')}) {c.get('username')}: {text[:40]}")

    # 私信
    if dm_enabled:
        try:
            new_cursor, dm_msgs = await fetch_private_inbox(cookies, my_mid, device_id, dm_cursor)
            dm_cursor = new_cursor
            for m in dm_msgs:
                if replied >= per_poll_limit:
                    break
                if not _can_add(m["key"], "dm"):
                    continue
                wl = dm_whitelist or []
                if wl and m["mid"] not in set(str(x) for x in wl):
                    state.mark(m["key"])
                    continue
                text = ""
                try:
                    text = await gen_reply(m)
                except Exception as e:
                    logger.warning(f"[bili_watcher] 生成私信回复异常: {e}")
                text = str(text or "").strip().strip('"\'“”‘’')
                if not text or len(text) > 300:
                    state.mark(m["key"])
                    continue
                ok = await send_private_msg(cookies, my_mid, m["mid"], text, device_id)
                if ok:
                    state.mark(m["key"])
                    state.add_sent(text)
                    replied += 1
                    replies.append({"username": m.get("username", ""), "channel": "dm", "text": text})
        except Exception as e:
            logger.warning(f"[bili_watcher] 私信轮询异常: {e}")
        state.save()

    return {"replied": replied, "replies": replies, "dm_cursor": dm_cursor}
