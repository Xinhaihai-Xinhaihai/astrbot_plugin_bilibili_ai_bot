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


async def fetch_video(bvid: str, frame_count: int) -> VideoContent:
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
        mp4 = await _download_video(bvid, work_video_dir())
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


async def _download_video(bvid: str, out_dir: Path) -> str | None:
    """yt-dlp 下载低清 mp4（最多 480p），失败返回 None。"""
    import sys

    out_dir = out_dir if out_dir else work_video_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = str(out_dir / f"{bvid}.%(ext)s")
    base = [sys.executable, "-m", "yt_dlp", "-o", tpl,
            "--no-playlist", "--merge-output-format", "mp4",
            "--limit-rate", "2M",
            "--add-header", "Referer: https://www.bilibili.com",
            "--no-warnings"]
    fmts = ["bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]",
            "best[height<=480][ext=mp4]", "best[height<=480]", "best"]
    for f in fmts:
        cmd2 = base + ["--format", f, f"https://www.bilibili.com/video/{bvid}"]
        logger.info(f"[bili_watcher] yt-dlp 下载 {bvid}: {f}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd2,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0:
                for ext in ("mp4", "mkv", "m4a", "webm", "flv"):
                    p = out_dir / f"{bvid}.{ext}"
                    if p.exists() and p.stat().st_size > 0:
                        return str(p)
                # 没匹配到就找 bvid 开头的文件
                for p in out_dir.iterdir():
                    if str(p.name).startswith(bvid) and p.suffix not in (".txt",):
                        return str(p)
                break
            else:
                logger.warning(f"[bili_watcher] yt-dlp 格式失败: {err.decode(errors='ignore')[:160]}")
        except asyncio.TimeoutError:
            logger.warning(f"[bili_watcher] yt-dlp 超时 {bvid}")
            break
        except Exception as e:
            logger.warning(f"[bili_watcher] yt-dlp 异常 {bvid}: {e}")
            break
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
