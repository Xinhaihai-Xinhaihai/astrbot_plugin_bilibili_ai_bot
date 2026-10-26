"""B站二维码登录：generate → 轮询 → 拿 cookie(SESSDATA/bili_jct/DedeUserID)。

直接走 B 站官方 Web 接口，不依赖 bilibili_api 的 login 模块。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from astrbot.api import logger

_QRCODE_API = "https://api.bilibili.com/x/web-interface/nav/qrcode/generate"
_POLL_API = "https://api.bilibili.com/x/web-interface/nav/qrcode/login"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _get_json(url: str, cookies: dict | None = None, headers: dict | None = None):
    import aiohttp

    h = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    if headers:
        h.update(headers)
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(cookies=cookies, timeout=timeout) as s:
        async with s.get(url, headers=h) as resp:
            return await resp.json(content_type=None)


async def generate_qrcode() -> tuple[str, str] | None:
    """返回 (二维码图片URL, 用于轮询的 key)。"""
    try:
        data = await _get_json(_QRCODE_API)
        if data.get("code") != 0:
            logger.warning(f"[bili_watcher] 生成二维码失败: {data}")
            return None
        d = data.get("data", {})
        return d.get("url", ""), d.get("key", "")
    except Exception as e:
        logger.warning(f"[bili_watcher] 生成二维码异常: {e}")
        return None


async def poll_qrcode(key: str, timeout: int = 120) -> dict[str, Any]:
    """轮询登录状态，直到成功/失败/超时。返回 cookies 相关字段 dict。

    返回:
      {"ok": bool, "info": str, "cookies": {SESSDATA, bili_jct, DedeUserID}}
    """
    import aiohttp

    result: dict[str, Any] = {"ok": False, "info": "超时未确认", "cookies": {}}
    cookies: dict[str, str] = {}
    deadline = asyncio.get_event_loop().time() + timeout
    async with aiohttp.ClientSession(cookies=cookies) as s:
        while asyncio.get_event_loop().time() < deadline:
            try:
                data = await _get_json(_POLL_API + f"?key={key}", cookies=cookies)
            except Exception as e:
                logger.warning(f"[bili_watcher] 轮询异常: {e}")
                await asyncio.sleep(2)
                continue
            code = data.get("code")
            if code == 0:
                result["ok"] = True
                result["info"] = "登录成功"
                result["cookies"] = cookies
                return result
            if code in (86090, 86038):  # 未扫码 / 二维码过期
                result["info"] = "等待扫码/二维码过期"
                if code == 86038:
                    break
            elif code == 86101:  # 已扫码待确认
                result["info"] = "已扫码，请在手机确认"
            elif code == 86077:  # 拒绝
                result["info"] = "已取消/拒绝"
                break
            else:
                result["info"] = f"状态码 {code}"
            await asyncio.sleep(2)
    return result


def cookies_to_strings(cookies: dict[str, str]) -> dict[str, str]:
    """把 aiohttp cookie dict 规整成 {SESSDATA, bili_jct, DedeUserID}。"""
    out = {}
    for k, v in (cookies or {}).items():
        if k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3"):
            out[k] = str(v)
    return out


def make_qrcode_png(url: str) -> str | None:
    """把二维码 URL 渲染成本地 PNG 路径。"""
    try:
        import qrcode
        from pathlib import Path
        import tempfile

        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        p = Path(tempfile.mktemp(prefix="bili_qr_", suffix=".png"))
        img.save(p)
        return str(p)
    except Exception as e:
        logger.warning(f"[bili_watcher] 渲染二维码失败: {e}")
        return None
