"""B站视频内容获取：yt-dlp 下载 → ffmpeg 抽帧 + 视频信息 + 弹幕。

不碰点赞/投币/关注——只读内容，为生成评论服务。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


@dataclass
class VideoContent:
    bvid: str
    aid: int = 0
    title: str = ""
    desc: str = ""
    duration: int = 0
    owner: str = ""
    cover: str = ""
    stat: dict = field(default_factory=dict)
    video_path: str = ""  # 下载的 mp4
    frames: list[str] = field(default_factory=list)  # 本地截图路径
    danmaku: list[str] = field(default_factory=list)
    top_comments: list[tuple[str, str, int]] = field(default_factory=list)  # (用户, 内容, 点赞)

    def work_dir(self) -> Path:
        if self.frames:
            return Path(self.frames[0]).parent
        if self.video_path:
            return Path(self.video_path).parent
        return Path(tempfile.gettempdir())


def parse_bvid(raw: str) -> str | None:
    """从用户输入里抠出 BV 号（支持整条链接 / 纯 BV）。"""
    raw = (raw or "").strip()
    m = _BV_RE.search(raw)
    return m.group(1) if m else None


async def fetch_video(bvid: str, frame_count: int, sessdata: str = "", bili_jct: str = "") -> VideoContent:
    """获取视频信息 + 下载 + 抽帧 + 弹幕 + 热门评论。信息拿不到才抛异常，其余失败兜底。"""
    from bilibili_api import video as blv

    vc = VideoContent(bvid=bvid)

    # 1) 基本信息（拿不到就抛，后面没意义）
    v = blv.Video(bvid=bvid)
    try:
        info = await v.get_info()
    except Exception as e:
        logger.warning(f"[bili_watcher] 获取视频信息失败 {bvid}: {e}")
        raise
    vc.aid = int(info.get("aid") or 0)
    vc.title = str(info.get("title") or "")
    vc.desc = str(info.get("desc") or "")
    vc.owner = str((info.get("owner") or {}).get("name") or "")
    vc.duration = int(info.get("duration") or 0)
    vc.cover = str(info.get("pic") or "")

    # 2) stat
    try:
        stat = await v.get_stat()
        vc.stat = stat if isinstance(stat, dict) else {}
    except Exception:
        pass

    # 3) 下载 + 抽帧（失败不致命）
    try:
        mp4 = await _download_video(bvid, work_video_dir(), sessdata, bili_jct)
        if mp4:
            vc.video_path = str(mp4)
            vc.frames = await _extract_frames(vc.video_path, vc.duration, frame_count)
        else:
            logger.warning(f"[bili_watcher] 视频下载失败 {bvid}（将只用标题/简介/弹幕）")
    except Exception as e:
        logger.warning(f"[bili_watcher] 抽帧失败 {bvid}: {e}（将只用标题/简介/弹幕）")
        vc.frames = []

    # 4) 弹幕
    try:
        dms = await v.get_danmakus()
        texts = []
        for d in dms or []:
            try:
                t = d.content if hasattr(d, "content") else d.get("content")
                if t and str(t).strip():
                    texts.append(str(t).strip())
            except Exception:
                continue
            if len(texts) >= 30:
                break
        vc.danmaku = texts
    except Exception as e:
        logger.debug(f"[bili_watcher] 取弹幕失败 {bvid}: {e}")

    # 5) 热门评论（老接口 /x/v2/reply 免 wbi，带 Referer）
    try:
        import aiohttp

        url = f"https://api.bilibili.com/x/v2/reply?oid={vc.aid}&sort=2&ps=8&pn=1&type=1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
            "Referer": f"https://www.bilibili.com/video/{bvid}",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.get(url) as r:
                d = await r.json(content_type=None)
        if d.get("code") == 0:
            reps = (d.get("data") or {}).get("replies") or []
            for x in reps[:8]:
                try:
                    msg = ((x.get("content") or {}).get("message") or "").strip()
                    name = ((x.get("member") or {}).get("uname") or "匿名")
                    like = int(x.get("like") or 0)
                    if msg:
                        vc.top_comments.append((name, msg, like))
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"[bili_watcher] 取评论失败 {bvid}: {e}")

    return vc


def work_video_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "bili_watcher"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _download_video(bvid: str, out_dir: Path, sessdata: str = "", bili_jct: str = "") -> str | None:
    """用 bilibili_api 拿 CDN 直链 → aiohttp 下载视频+音频 → ffmpeg 合并 mp4。

    不走 www.bilibili.com 页面（数据中心 IP 会被 412 风控），直接拉 CDN。
    失败返回 None。
    """
    from bilibili_api import video as blv
    from bilibili_api import Credential
    import aiohttp

    out_dir = out_dir if out_dir else work_video_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    cred = None
    if sessdata or bili_jct:
        cred = Credential(sessdata=sessdata, bili_jct=bili_jct)
    v = blv.Video(bvid=bvid, credential=cred)
    try:
        d = await v.get_download_url(0)
    except Exception as e:
        logger.warning(f"[bili_watcher] get_download_url 失败 {bvid}: {e}")
        return None

    dash = (d or {}).get("dash") or {}
    videos = dash.get("video") or []
    if not videos:
        logger.warning(f"[bili_watcher] 无 dash video {bvid}")
        return None

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    hdrs = {"User-Agent": ua, "Referer": f"https://www.bilibili.com/video/{bvid}"}

    # 挑 ≤480p 的最清晰视频流
    cand = sorted(videos, key=lambda x: (abs((x.get("height") or 999) - 480), (x.get("id") or 0)))
    picked = cand[0]
    vurl = picked.get("baseUrl") or picked.get("base_url")
    if not vurl:
        logger.warning(f"[bili_watcher] 无视频直链 {bvid}")
        return None

    vpath = out_dir / f"{bvid}_v.m4s"
    apath = out_dir / f"{bvid}_a.m4s"

    async def _dl(url: str, dest: Path, timeout: int = 180) -> bool:
        try:
            async with aiohttp.ClientSession(headers=hdrs, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        logger.warning(f"[bili_watcher] CDN {r.status} {bvid}")
                        return False
                    with open(dest, "wb") as f:
                        async for chunk in r.content.iter_chunked(256 * 1024):
                            f.write(chunk)
            return dest.exists() and dest.stat().st_size > 0
        except Exception as e:
            logger.warning(f"[bili_watcher] CDN 下载异常 {bvid}: {e}")
            return False

    okv = await _dl(vurl, vpath)
    # 音频（DASH 通常视频音频分离）
    aurl = None
    auds = dash.get("audio") or []
    if auds:
        aurl = auds[0].get("baseUrl") or auds[0].get("base_url")
    oka = False
    if aurl:
        oka = await _dl(aurl, apath)

    if not okv:
        for p in (vpath, apath):
            try:
                p.unlink()
            except Exception:
                pass
        return None

    # 合并
    final = out_dir / f"{bvid}.mp4"
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"
    cmd = [ffmpeg, "-y"]
    if oka and apath.exists():
        cmd += ["-i", str(vpath), "-i", str(apath), "-c:v", "copy", "-c:a", "aac", "-strict", "experimental"]
    else:
        cmd += ["-i", str(vpath), "-c:v", "copy"]
    cmd += [str(final)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except Exception as e:
        logger.warning(f"[bili_watcher] ffmpeg 合并异常 {bvid}: {e}")
        proc = None
        err = b""
    if final.exists() and final.stat().st_size > 0:
        for p in (vpath, apath):
            try:
                p.unlink()
            except Exception:
                pass
        return str(final)
    # 合并失败就退回纯视频流（只抽帧不需要音频）
    if vpath.exists() and vpath.stat().st_size > 0:
        logger.warning(f"[bili_watcher] ffmpeg 合并失败，退回视频流 {bvid}: {err.decode(errors='ignore')[:120]}")
        for p in (apath,):
            try:
                p.unlink()
            except Exception:
                pass
        return str(vpath)
    return None


async def _extract_frames(video_path: str, duration: int, frame_count: int) -> list[str]:
    """按时间点均匀抽 N 帧（480p 宽，省 token）。"""
    import time

    try:
        import imageio_ffmpeg
    except Exception:
        logger.warning("[bili_watcher] 缺 imageio-ffmpeg，无法抽帧")
        return []
    duration = int(duration or 0)
    if duration <= 0 or not video_path or not os.path.exists(video_path):
        return []
    work = Path(video_path).parent
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tag = f"frame_{int(time.time())}"
    frames: list[str] = []
    for i in range(frame_count):
        frac = (i + 1) / (frame_count + 1)
        t = duration * frac
        out = work / f"{tag}_{i}.jpg"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{t:.2f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", "scale=480:-2",
            "-q:v", "4", str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            continue
        if out.exists() and out.stat().st_size > 0:
            frames.append(str(out))
    return frames


async def send_comment(aid: int, text: str, sessdata: str, bili_jct: str) -> dict:
    """以登录身份发一条视频评论（aid 维度）。"""
    from bilibili_api import Credential, comment

    cred = Credential(sessdata=sessdata, bili_jct=bili_jct)
    return await comment.send_comment(
        text=text,
        oid=aid,
        type_=comment.ResourceType.VIDEO,
        credential=cred,
    )


def cleanup(vc: VideoContent) -> None:
    """删掉下载的视频和抽帧临时文件。"""
    try:
        seen = set()
        paths: list[Path] = []
        if vc.video_path:
            paths.append(Path(vc.video_path))
        paths.extend(Path(p) for p in vc.frames)
        for p in paths:
            if p in seen or not p.exists():
                continue
            seen.add(p)
            try:
                p.unlink()
            except Exception:
                pass
        # 顺手清一下工作目录里过老的临时文件
        d = work_video_dir()
        if d.exists():
            import time
            now = time.time()
            for f in d.iterdir():
                try:
                    if now - f.stat().st_mtime > 3600 and f.name.startswith(vc.bvid):
                        f.unlink()
                except Exception:
                    pass
    except Exception:
        pass
