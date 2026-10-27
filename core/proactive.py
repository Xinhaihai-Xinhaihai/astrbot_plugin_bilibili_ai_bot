"""主动看片：每隔 N 分钟自己挑个视频看，看完像真人一样评论 / 发动态。

流程：
  挑候选（热门池 + 按口味关键词搜索 + 特别关注 UP）→ 下载抽帧喂视觉模型
  → LLM 严格 JSON 评估（分数 / 想不想评 / 想不想发动态）→ 发评论或发动态
  → 截图推送 notify_groups。

只复用已有能力：下载抽帧走 bilibili.fetch_video，发评论走 bilibili.send_comment，
发动态走 bilibili_api.dynamic.send_dynamic，视觉模型链走 commenter._try_provider_raw。
冷却和互动轮询共用同一个 cooldown.json / interact_state.json 所在目录。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_EVAL_KEYS = {
    "score", "score_reason", "comment", "mood", "review", "want_comment",
    "recommend_owner", "recommend_reason", "want_dynamic", "dynamic_text",
    "partition", "search_keywords",
}
_EVAL_SCHEMA_PROMPT = (
    "只输出一个完整JSON对象，不要Markdown、解释或额外字段："
    '{"score":7.0,"score_reason":"评分理由","comment":"想发在评论区的短句或空字符串","mood":"开心|平静|无聊|感动|好笑|震撼|困惑","review":"个人感想","want_comment":false,"recommend_owner":false,"recommend_reason":"","want_dynamic":false,"dynamic_text":"","partition":"实际分区","search_keywords":["以后真能拿去B站搜索的具体词"]}\n'
    "score 是观感 1-10；want_comment 表示自己是否真想在这视频评论区留一句，"
    "为 true 时 comment 写具体内容，否则留空；大多数视频 want_comment=false。"
    "want_dynamic 表示看完是否真有话想在自己的 B 站动态里说，大多数视频 false，"
    "为 true 时 dynamic_text 写想说的具体内容（40字内）。"
    "最多 5 个搜索词，优先作品/人物/作者/主题/技术，不要写“动漫”“游戏”这种过宽词。"
)

_MIN_COMMENT_LEN = 4


# ── 数据结构 ──

@dataclass
class Candidate:
    bvid: str
    title: str
    up_name: str = ""
    up_mid: str = ""
    source: str = "pool"  # popular / search / follow
    source_detail: str = ""
    score_hint: int = 0
    extra: dict = field(default_factory=dict)


# ── 配置读取 ──

def _cfg_get(cfg, key, default=None):
    try:
        v = cfg.get(key, default) if cfg is not None else default
        return default if v is None else v
    except Exception:
        return default


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = v.replace("，", ",").replace(" ", ",")
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    return []


# ── WBI 签名 + HTTP ──

def _mixin_key(orig: str) -> str:
    r = ""
    for i in _MIXIN_KEY_ENC_TAB:
        r += orig[i]
    return r[:32]


def _wbi_sign(params: dict, ik: str, sk: str) -> dict:
    import hashlib
    import time

    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    mk = _mixin_key(ik + sk)
    params["w_rid"] = hashlib.md5(
        ("&".join(f"{k}={v}" for k, v in params.items()) + mk).encode()
    ).hexdigest()
    return params


async def _http_get_json(url, params=None, headers=None, cookies=None, timeout=15):
    import aiohttp

    h = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    if headers:
        h.update(headers)
    async with aiohttp.ClientSession(
        cookies=cookies or None, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as s:
        async with s.get(url, params=params, headers=h) as r:
            txt = await r.text()
    try:
        return json.loads(txt)
    except Exception:
        return {"code": -1, "message": txt[:200]}


async def _get_wbi_keys(sessdata: str):
    d = await _http_get_json(
        "https://api.bilibili.com/x/web-interface/nav",
        cookies={"SESSDATA": sessdata} if sessdata else None,
    )
    wbi = (d.get("data") or {}).get("wbi_img") or {}
    ik = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sk = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    return ik, sk


async def _wbi_search(sessdata: str, keyword: str, ps: int) -> list[Candidate]:
    """匿名/登录态 WBI 搜索视频，返回候选。失败返回空。"""
    ik, sk = await _get_wbi_keys(sessdata)
    if not ik or not sk:
        return []
    params = {
        "keyword": keyword, "search_type": "video",
        "page": 1, "page_size": ps, "order": "totalrank",
    }
    signed = _wbi_sign(params, ik, sk)
    d = await _http_get_json(
        "https://api.bilibili.com/x/web-interface/wbi/search/type",
        params=signed,
        cookies={"SESSDATA": sessdata} if sessdata else None,
    )
    if d.get("code") != 0:
        logger.debug(f"[bili_proactive] 搜索失败 {keyword}: code={d.get('code')}")
        return []
    out: list[Candidate] = []
    for v in ((d.get("data") or {}).get("result") or [])[:ps]:
        bvid = str(v.get("bvid") or "").strip()
        if not bvid:
            continue
        title = re.sub(r"<[^>]+>", "", str(v.get("title") or ""))
        out.append(Candidate(
            bvid=bvid, title=title,
            up_name=str(v.get("author") or ""), up_mid=str(v.get("mid") or ""),
            source="search", source_detail=keyword,
            score_hint=int(v.get("play") or 0),
        ))
    return out


async def _popular_pool(sessdata: str, ps: int) -> list[Candidate]:
    """热门池，匿名可用（实测可用，优先用）。"""
    d = await _http_get_json(
        "https://api.bilibili.com/x/web-interface/popular",
        params={"ps": min(ps, 50), "pn": random.randint(1, 4)},
        cookies={"SESSDATA": sessdata} if sessdata else None,
    )
    if d.get("code") != 0:
        logger.debug(f"[bili_proactive] 热门池失败: code={d.get('code')}")
        return []
    out: list[Candidate] = []
    for v in ((d.get("data") or {}).get("list") or []):
        bvid = str(v.get("bvid") or "").strip()
        if not bvid:
            continue
        out.append(Candidate(
            bvid=bvid, title=str(v.get("title") or ""),
            up_name=str((v.get("owner") or {}).get("name") or ""),
            up_mid=str((v.get("owner") or {}).get("mid") or ""),
            source="pool",
            score_hint=int((v.get("stat") or {}).get("view") or 0),
        ))
    return out


async def _up_latest_video(uid: str, sessdata: str, bili_jct: str) -> Candidate | None:
    """取某个 UP 最新投稿，给特别关注用。失败返回 None。"""
    try:
        from bilibili_api import user as u
        from bilibili_api import Credential
    except Exception:
        return None
    uid = str(uid).strip()
    if not uid.isdigit():
        return None
    cred = Credential(sessdata=sessdata, bili_jct=bili_jct)
    try:
        d = await u.User(uid=int(uid), credential=cred).get_videos(pn=1)
    except Exception as e:
        logger.debug(f"[bili_proactive] UP {uid} 取视频失败: {e}")
        return None
    vlist = d.get("list") or d.get("vlist") or []
    if not vlist:
        return None
    v = vlist[0]
    return Candidate(
        bvid=str(v.get("bvid") or ""), title=str(v.get("title") or ""),
        up_name=str(v.get("author") or ""), up_mid=uid,
        source="follow", source_detail=uid,
    )


# ── 候选收集 ──

async def _collect_candidates(cfg, sessdata: str, bili_jct: str,
                              want: int) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []

    def add_all(items: list[Candidate]):
        for c in items:
            if c.bvid and c.bvid not in seen:
                seen.add(c.bvid)
                out.append(c)

    # 1) 特别关注 UP（登录态）
    follow_uids = _as_list(_cfg_get(cfg, "proactive_follow_uids", []))
    for uid in follow_uids:
        if len(out) >= want:
            break
        c = await _up_latest_video(uid, sessdata, bili_jct)
        if c:
            add_all([c])
        await asyncio.sleep(random.uniform(0.3, 0.8))

    # 2) 口味关键词搜索（登录态，没有就匿名也能搜）
    keywords = _as_list(_cfg_get(cfg, "proactive_keywords", []))
    if keywords:
        for kw in keywords[:3]:
            if len(out) >= want * 2:
                break
            add_all(await _wbi_search(sessdata, kw, ps=20))
            await asyncio.sleep(random.uniform(0.2, 0.5))

    # 3) 热门池兜底（匿名可用）
    if len(out) < want:
        add_all(await _popular_pool(sessdata, ps=50))

    random.shuffle(out)
    return out


# ── 视觉模型评估 ──

async def _evaluate(context, cfg, vc) -> dict | None:
    """喂标题/简介/弹幕/截图给视觉模型，返回解析后的评估 dict，失败返回 None。"""
    from .commenter import _try_provider_raw, _build_prompt  # noqa: F401

    timeout = int(_cfg_get(cfg, "vision_timeout_seconds", 120) or 120)
    main_pid = str(_cfg_get(cfg, "vision_provider_id", "") or "").strip()
    main_model = str(_cfg_get(cfg, "vision_model", "") or "").strip()
    fallbacks = _as_list(_cfg_get(cfg, "vision_fallback_provider_ids", []))

    prompt = _build_prompt(vc)
    sys_prompt = (
        "你在 B 站，刚刷到一个视频，在想要不要评论/发动态。\n"
        + _EVAL_SCHEMA_PROMPT
    )
    image_urls = [str(p) for p in vc.frames] or None

    raw = ""
    if main_pid or main_model:
        raw = await _try_provider_raw(context, main_pid, main_model, prompt, sys_prompt, image_urls, timeout)
    if not raw:
        for fid in fallbacks:
            raw = await _try_provider_raw(context, fid, "", prompt, sys_prompt, image_urls, timeout)
            if raw:
                break
    if not raw:
        raw = await _try_provider_raw(context, "", "", prompt, sys_prompt, image_urls, timeout)
    if not raw:
        return None

    return _parse_eval(raw)


def _parse_eval(raw: str) -> dict | None:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # 只取第一个完整 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict) or "score" not in d:
        return None

    def _s(key, default="", limit=200):
        v = str(d.get(key, "") or "").strip()
        return v[:limit]

    score = d.get("score")
    try:
        score = round(float(score), 1)
        if not 1.0 <= score <= 10.0:
            return None
    except Exception:
        return None

    return {
        "score": score,
        "score_reason": _s("score_reason"),
        "comment": _s("comment", limit=80).strip("'\"“”‘’ "),
        "review": _s("review", limit=180),
        "want_comment": bool(d.get("want_comment", False)),
        "recommend_owner": bool(d.get("recommend_owner", False)),
        "recommend_reason": _s("recommend_reason", limit=80),
        "want_dynamic": bool(d.get("want_dynamic", False)),
        "dynamic_text": _s("dynamic_text", limit=80),
        "partition": _s("partition", limit=30),
        "search_keywords": [str(k)[:40] for k in (d.get("search_keywords") or []) if str(k).strip()][:5],
    }


# ── 评论有效性 ──

def _valid_comment(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < _MIN_COMMENT_LEN:
        return False
    return bool(re.search(r"[0-9A-Za-z一-鿿]", t))


# ── 主动看片日志 ──

def _log_file() -> Path:
    d = Path(StarTools.get_data_dir("astrbot_plugin_bili_watcher"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "proactive_log.json"


def _load_log() -> list:
    p = _log_file()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_log(entries: list):
    try:
        _log_file().write_text(
            json.dumps(entries[-500:], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ── 主入口 ──

async def run_proactive_round(context, cfg, notify=None, store=None) -> dict:
    """跑一轮主动看片。返回 {'commented': N, 'dynamic': N, 'skipped': 原因, 'title': ...}"""
    sessdata = str(_cfg_get(cfg, "sessdata", "") or "").strip()
    bili_jct = str(_cfg_get(cfg, "bili_jct", "") or "").strip()
    if not (sessdata and bili_jct):
        return {"commented": 0, "dynamic": 0, "skipped": "未登录"}

    min_comment_score = float(_cfg_get(cfg, "proactive_comment_min_score", 6) or 6)
    allow_dynamic = bool(_cfg_get(cfg, "proactive_dynamic", True))
    want = max(1, int(_cfg_get(cfg, "proactive_video_count", 1) or 1))
    watch_cooldown_hours = float(_cfg_get(cfg, "proactive_video_cooldown_hours", 48) or 48)

    log = _load_log()
    seen_bvids = {e.get("bvid") for e in log if e.get("bvid")}

    candidates = await _collect_candidates(cfg, sessdata, bili_jct, want=want * 3)
    # 过滤掉已处理过的 + 冷却中的
    fresh: list[Candidate] = []
    for c in candidates:
        if c.bvid in seen_bvids:
            continue
        if store is not None and store.in_video_cooldown(c.bvid, watch_cooldown_hours) > 0:
            continue
        fresh.append(c)
    candidates = fresh
    if not candidates:
        return {"commented": 0, "dynamic": 0, "skipped": "没有新候选（都看过了/冷却中）"}

    from .bilibili import cleanup, fetch_video, send_comment

    result = {"commented": 0, "dynamic": 0, "skipped": "", "title": ""}
    for cand in candidates[: want * 2]:
        try:
            vc = await fetch_video(cand.bvid, int(_cfg_get(cfg, "frame_count", 6) or 6))
        except Exception as e:
            logger.debug(f"[bili_proactive] 取视频失败 {cand.bvid}: {e}")
            continue

        try:
            ev = await _evaluate(context, cfg, vc)
        except Exception as e:
            logger.warning(f"[bili_proactive] 评估 {cand.bvid} 失败: {e}")
            ev = None
        finally:
            comment_text = ev.get("comment", "") if ev else ""
            review = ev.get("review", "") if ev else ""
            score = ev.get("score", 0) if ev else 0
            want_comment = bool(ev.get("want_comment")) if ev else False
            want_dynamic = bool(ev.get("want_dynamic")) if ev else False
            dynamic_text = ev.get("dynamic_text", "") if ev else ""
            partition = ev.get("partition", "") if ev else ""
            search_keywords = ev.get("search_keywords", []) if ev else []
            top_cmts = list(vc.top_comments)

        if not ev:
            cleanup(vc)
            continue

        did_comment = False
        did_dynamic = False

        # 发评论
        if want_comment and score >= min_comment_score and _valid_comment(comment_text):
            try:
                await send_comment(vc.aid, comment_text, sessdata, bili_jct)
                did_comment = True
                result["commented"] += 1
                logger.info(f"[bili_proactive] 评论成功 {cand.bvid}: {comment_text[:30]}")
                if store is not None:
                    store.mark(cand.bvid)
            except Exception as e:
                logger.warning(f"[bili_proactive] 发评论失败 {cand.bvid}: {e}")

        # 发动态
        if allow_dynamic and want_dynamic and dynamic_text and not did_comment:
            try:
                await _send_dynamic(sessdata, bili_jct, dynamic_text, cand, review)
                did_dynamic = True
                result["dynamic"] += 1
                logger.info(f"[bili_proactive] 发动态 {cand.bvid}: {dynamic_text[:30]}")
                if store is not None:
                    store.mark(cand.bvid)
            except Exception as e:
                logger.warning(f"[bili_proactive] 发动态失败 {cand.bvid}: {e}")

        # 记日志
        log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bvid": cand.bvid, "title": cand.title or vc.title,
            "up_name": cand.up_name or vc.owner,
            "source": cand.source, "source_detail": cand.source_detail,
            "score": score, "partition": partition, "review": review,
            "comment": comment_text if did_comment else "",
            "dynamic": dynamic_text if did_dynamic else "",
            "search_keywords": search_keywords,
        })
        _save_log(log)

        result["title"] = cand.title or vc.title
        result["bvid"] = cand.bvid

        # 推送通知
        if notify is not None and (did_comment or did_dynamic):
            src_note = {"pool": "热门", "search": "搜索", "follow": "关注"}.get(cand.source, cand.source)
            if did_comment:
                body = f"主动刷B站看完《{cand.title}》\n[{src_note}] 评分 {score} | {partition}\n评论：{comment_text}\nhttps://www.bilibili.com/video/{cand.bvid}"
            else:
                body = f"主动刷B站看完《{cand.title}》\n[{src_note}] 评分 {score} | {partition}\n发了动态：{dynamic_text}"
            try:
                await notify(body)
            except Exception as e:
                logger.warning(f"[bili_proactive] 通知失败: {e}")

        cleanup(vc)

        if did_comment or did_dynamic:
            return result

    return result


async def _send_dynamic(sessdata: str, bili_jct: str, text: str,
                        cand: Candidate, review: str = "") -> dict:
    from bilibili_api import Credential, dynamic

    cred = Credential(sessdata=sessdata, bili_jct=bili_jct)
    # B 站动态里贴链接，方便点进原视频
    body = text.strip()
    return await dynamic.send_dynamic(text=body, credential=cred)
