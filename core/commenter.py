"""基于视频内容生成一条像真人的短评。

视觉模型可配置 + 多级回退：
  1) vision_provider_id / vision_model  —— 主视觉模型（支持图片输入）
  2) vision_fallback_provider_ids     —— 主模型挂了按顺序试
  3) 当前正在使用的提供商              —— 兜底
不点赞、不刷量，只产出一条评论文本，由调用方确认后发出。
"""

from __future__ import annotations

from astrbot.api import logger

from .bilibili import VideoContent

_SYSTEM_PROMPT = (
    "你在 B 站，刚看完一个视频，想随手留一条评论。"
    "要求：{style}\n"
    "只输出评论本身，不要引号、不要解释、不要'我觉得'开头这种套路。"
)


def _build_prompt(content: VideoContent) -> str:
    parts = [f"标题：{content.title or '（无）'}"]
    if content.desc:
        parts.append(f"简介：{content.desc[:300]}")
    if content.owner:
        parts.append(f"UP主：{content.owner}")
    if content.danmaku:
        dm = " ｜ ".join(content.danmaku[:12])
        parts.append(f"部分弹幕：{dm}")
    if content.top_comments:
        tc = " ；".join(f"{u}：{t}" for u, t, _ in content.top_comments[:6])
        parts.append(f"热门评论：{tc}")
    n_frame = len(content.frames)
    if n_frame:
        parts.append(f"（附 {n_frame} 张视频截图，请结合画面理解内容）")
    else:
        parts.append("（没有截图，仅凭标题/简介/弹幕判断）")
    parts.append("请写一条评论。")
    return "\n".join(parts)


def _clean(text: str) -> str:
    text = (text or "").strip().strip("\"'“”‘’")
    if len(text) > 80:
        text = text[:80]
    return text


async def _try_provider(context, provider_id: str, model: str, prompt: str, sys_prompt: str,
                        image_urls: list[str] | None, timeout: int) -> str:
    """用指定提供商调一次，失败返回空串。"""
    try:
        from astrbot.core.provider.manager import ProviderManager  # noqa: F401
    except Exception:
        pass

    provider = None
    try:
        mgr = getattr(context, "provider_manager", None)
        if mgr is None:
            return ""
        if provider_id:
            prov = mgr.insts.get(provider_id) if hasattr(mgr, "insts") else None
            if prov is None:
                for p in mgr.insts.values():
                    if getattr(p, "name", "") == provider_id or str(getattr(p, "id", "")) == provider_id:
                        prov = p
                        break
            provider = prov
        else:
            from astrbot.core.provider.manager import ProviderType
            provider = await mgr.get_using_provider_async(ProviderType.CHAT_COMPLETION)
    except Exception as e:
        logger.debug(f"[bili_watcher] 取提供商 {provider_id} 失败: {e}")
        return ""

    if provider is None:
        return ""

    try:
        import asyncio

        coro = provider.text_chat(
            prompt=prompt,
            system_prompt=sys_prompt,
            image_urls=image_urls,
            model=model or None,
        )
        resp = await asyncio.wait_for(coro, timeout=timeout)
        return _clean(getattr(resp, "completion_text", "") or "")
    except Exception as e:
        logger.warning(f"[bili_watcher] 提供商 {provider_id or '<当前>'} 调用失败: {e}")
        return ""


async def generate_comment(
    content: VideoContent,
    style: str,
    context,
    cfg=None,
) -> str:
    """按配置顺序尝试主视觉模型 → 回退模型 → 当前提供商，返回评论文本。"""
    user_prompt = _build_prompt(content)
    sys_prompt = _SYSTEM_PROMPT.format(style=style or "口语、短、自然")
    image_urls = [str(p) for p in content.frames] or None

    def _g(key, default=None):
        try:
            return cfg.get(key, default) if cfg is not None else default
        except Exception:
            return default

    timeout = int(_g("vision_timeout_seconds", 120) or 120)
    main_pid = str(_g("vision_provider_id", "") or "").strip()
    main_model = str(_g("vision_model", "") or "").strip()
    fallbacks = _g("vision_fallback_provider_ids", []) or []
    if isinstance(fallbacks, str):
        fallbacks = [f.strip() for f in fallbacks.replace("，", ",").split(",") if f.strip()]
    fallbacks = [str(f).strip() for f in fallbacks if str(f).strip()]

    # 1) 主视觉模型
    if main_pid or main_model:
        r = await _try_provider(context, main_pid, main_model, user_prompt, sys_prompt, image_urls, timeout)
        if r:
            logger.info(f"[bili_watcher] 主视觉模型 {main_pid or '<当前>'} 生成评论成功")
            return r

    # 2) 回退模型
    for fid in fallbacks:
        r = await _try_provider(context, fid, "", user_prompt, sys_prompt, image_urls, timeout)
        if r:
            logger.info(f"[bili_watcher] 回退视觉模型 {fid} 生成评论成功")
            return r

    # 3) 当前使用的提供商
    r = await _try_provider(context, "", "", user_prompt, sys_prompt, image_urls, timeout)
    if r:
        logger.info("[bili_watcher] 当前提供商生成评论成功")
        return r

    logger.warning("[bili_watcher] 所有视觉模型都失败了，评论没生成")
    return ""
