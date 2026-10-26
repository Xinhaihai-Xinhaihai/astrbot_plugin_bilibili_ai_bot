"""把视频信息渲染成可爱风卡片：套用 assets/template.jpg 相框，中间留白区填内容。

留白区实测（1024x1536 模板）：x 180~880、y 235~990 为干净区域，
左右花束装饰和底部人物都不会被盖到。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

_TEMPLATE = Path(__file__).parent.parent / "assets" / "template.jpg"

# 留白安全区
X0, Y0, X1, Y1 = 180, 235, 880, 990

# 配色（跟模板的蓝白粉呼应）
C_TITLE = (92, 108, 148)
C_META = (140, 150, 178)
C_TEXT = (86, 96, 130)
C_NAME = (150, 158, 190)
C_LIKE = (238, 148, 168)
C_MY = (74, 132, 168)
C_MY_TXT = (58, 110, 148)
C_LINK = (160, 170, 198)


def _load_font(px: int, bold: bool = False):
    from PIL import ImageFont

    px = max(12, int(px))
    if bold:
        cands = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]
    else:
        cands = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyhbd.ttc"]
    for c in cands:
        try:
            return ImageFont.truetype(c, px)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    text = (text or "").replace("\n", " ")
    lines, cur = [], ""
    for ch in text:
        test = cur + ch
        if draw.textlength(test, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines or [""]


async def _download_image(url: str, dest: Path) -> bool:
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False
                dest.write_bytes(await resp.read())
                return True
    except Exception as e:
        logger.debug(f"[bili_watcher] 下载封面失败 {url}: {e}")
        return False


def _rounded(im, radius: int):
    """加圆角 + 淡蓝描边。"""
    from PIL import Image, ImageDraw

    scale = 3
    mask = Image.new("L", (im.width * scale, im.height * scale), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, im.width * scale, im.height * scale], radius * scale, fill=255)
    mask = mask.resize((im.width, im.height), Image.LANCZOS)

    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im.convert("RGBA"), (0, 0), mask)

    border = Image.new("RGBA", (im.width + 8, im.height + 8), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle([0, 0, im.width + 7, im.height + 7], radius + 4, outline=(178, 200, 232, 255), width=3)
    border.paste(out, (4, 4), mask)
    return border


async def compose_screenshot(
    *,
    cover_url: str,
    frames: list[str],
    title: str,
    owner: str,
    bvid: str,
    comment: str,
    stat: dict | None = None,
    top_comments: list[tuple[str, str, int]] | None = None,
) -> str:
    """把视频信息塞进模板留白区，返回本地图片路径。"""
    import tempfile

    from PIL import Image, ImageDraw

    if not _TEMPLATE.exists():
        logger.warning("[bili_watcher] 模板图缺失，跳过截图")
        return ""

    try:
        canvas = Image.open(_TEMPLATE).convert("RGB")
    except Exception as e:
        logger.warning(f"[bili_watcher] 读模板失败: {e}")
        return ""

    draw = ImageDraw.Draw(canvas)
    tw = X1 - X0
    y = Y0

    f_title = _load_font(34, bold=True)
    f_meta = _load_font(21)
    f_body = _load_font(22)
    f_name = _load_font(19)
    f_sec = _load_font(20, bold=True)
    f_link = _load_font(18)

    # ---------- 封面（居中圆角） ----------
    cover_h = int(tw * 9 / 16)
    cover_img = None
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    if cover_url and await _download_image(cover_url, tmp):
        try:
            cover_img = Image.open(tmp).convert("RGB")
        except Exception:
            cover_img = None
        finally:
            tmp.unlink(missing_ok=True)
    if cover_img is None:
        for f in frames or []:
            try:
                cover_img = Image.open(f).convert("RGB")
                break
            except Exception:
                continue
    if cover_img is None:
        cover_img = Image.new("RGB", (tw, cover_h), (216, 228, 244))

    cw, ch = cover_img.size
    scale = max(tw / cw, cover_h / ch)
    cover_img = cover_img.resize((int(cw * scale) + 1, int(ch * scale) + 1))
    left = (cover_img.width - tw) // 2
    top = (cover_img.height - cover_h) // 2
    cover_img = cover_img.crop((left, top, left + tw, top + cover_h))
    cover_img = _rounded(cover_img, 14)
    canvas.paste(cover_img, (X0 - 4, y), cover_img)
    y += cover_h + 18

    # ---------- 标题 ----------
    for line in _wrap(draw, title or "(无标题)", f_title, tw)[:3]:
        draw.text((X0, y), line, font=f_title, fill=C_TITLE)
        y += int(f_title.size * 1.32)
    y += 6

    # ---------- UP主 + 数据 ----------
    draw.text((X0, y), f"UP主：{owner or '未知'}", font=f_meta, fill=C_META)
    y += int(f_meta.size * 1.5)
    stat_line = _stat_text(stat)
    if stat_line:
        draw.text((X0, y), stat_line, font=f_meta, fill=C_META)
        y += int(f_meta.size * 1.5)
    y += 10

    # ---------- 分隔虚线 ----------
    _dash(draw, X0, y, X1, y, (206, 218, 238))
    y += 16

    # ---------- 热门评论 ----------
    top_comments = (top_comments or [])[:4]
    if top_comments:
        draw.text((X0, y), "♡ 热门评论", font=f_sec, fill=(214, 128, 152))
        y += int(f_sec.size * 1.7)
        for name, text, like in top_comments:
            lines = _wrap(draw, text, f_body, tw - 46)
            draw.text((X0 + 2, y + 2), f"♥ {like:,}" if like else "♥", font=f_name, fill=C_LIKE)
            draw.text((X0 + 46, y + 2), name[:8] or "匿名", font=f_name, fill=C_NAME)
            for i, line in enumerate(lines[:3]):
                draw.text((X0 + 46, y + int(f_name.size * 1.5) + i * int(f_body.size * 1.3)), line, font=f_body, fill=C_TEXT)
            y += int(f_name.size * 1.5) + min(len(lines), 3) * int(f_body.size * 1.3) + 10
        y += 4
        _dash(draw, X0, y, X1, y, (206, 218, 238))
        y += 16

    # ---------- 我的评论 ----------
    draw.text((X0, y), "✿ 我发的评论", font=f_sec, fill=C_MY)
    y += int(f_sec.size * 1.7)
    for line in _wrap(draw, comment or "(未生成)", f_body, tw - 20)[:4]:
        draw.text((X0 + 8, y), line, font=f_body, fill=C_MY_TXT)
        y += int(f_body.size * 1.36)
    y += 12

    # ---------- 链接 ----------
    if y < Y1:
        draw.text((X0, y), f"▸ https://www.bilibili.com/video/{bvid}", font=f_link, fill=C_LINK)

    out = Path(tempfile.mktemp(prefix="bili_shot_", suffix=".jpg"))
    canvas.save(out, quality=90)
    return str(out)


def _dash(draw, x0, y0, x1, y1, color, width=2, dash=9, gap=7):
    x = x0
    while x < x1:
        draw.line([(x, y0), (min(x + dash, x1), y1)], fill=color, width=width)
        x += dash + gap


def _stat_text(stat: dict | None) -> str:
    if not stat:
        return ""
    parts = []
    for key, label in (("view", "播放"), ("like", "点赞"), ("reply", "评论"), ("favorite", "收藏"), ("coin", "投币"), ("share", "分享")):
        try:
            v = stat.get(key)
            if v is not None:
                parts.append(f"{label} {int(v):,}")
        except Exception:
            continue
    return "    ".join(parts)
