"""astrbot_plugin_bili_watcher — B站陪看评论.

发 B 站链接（或 BV 号）→ bot 下载视频抽帧喂多模态模型"看懂" → 生成一条像真人的
短评 → 用你的登录身份发出去 → 把「封面+标题+评论」截图推送到指定群给你看。

不点赞、不投币、不刷量。评论频率受「同视频冷却 + 全局间隔」双重限制，像真人。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.auth import cookies_to_strings, generate_qrcode, make_qrcode_png, poll_qrcode
from .core.bilibili import cleanup, fetch_video, parse_bvid, send_comment
from .core.commenter import generate_comment
from .core.cooldown import CooldownStore
from .core.screenshot import compose_screenshot


@register(
    "astrbot_plugin_bili_watcher",
    "心海海",
    "B站陪看评论 — 看视频(抽帧喂视觉模型)后发一条像真人的短评，评论完截图推送到指定群。不点赞不刷量。",
    "0.1.0",
    "https://github.com/Xinhaihai-Xinhaihai/astrbot_plugin_bili_watcher",
)
class BiliWatcherPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.cfg = config
        self._store: CooldownStore | None = None
        self._qr: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._reload_runtime()

    # ================= 配置 =================

    def _c(self, key: str, default=None):
        try:
            v = self.cfg.get(key, default) if self.cfg is not None else default
            return default if v is None else v
        except Exception:
            return default

    def _reload_runtime(self):
        try:
            data_dir = Path(StarTools.get_data_dir("astrbot_plugin_bili_watcher"))
            data_dir.mkdir(parents=True, exist_ok=True)
            self._store = CooldownStore(data_dir / "cooldown.json")
        except Exception as e:
            logger.warning(f"[bili_watcher] 初始化数据目录失败: {e}")
            self._store = None

    # ================= 工具 =================

    def _platform_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_id() or "")
        except Exception:
            return ""

    async def _push_groups(self, event: AstrMessageEvent, chain: MessageChain) -> list[str]:
        """把消息推送到配置里的群（评论截图通知用）。返回成功推送的群列表。"""
        groups = self._c("notify_groups", [])
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.replace("，", ",").split(",") if g.strip()]
        groups = [str(g).strip() for g in (groups or []) if str(g).strip()]
        pid = self._platform_id(event) or "default"
        ok = []
        for gid in groups:
            if not gid.isdigit():
                continue
            try:
                session = f"{pid}:GroupMessage:{gid}"
                await self.context.send_message(session, chain)
                ok.append(gid)
            except Exception as e:
                logger.warning(f"[bili_watcher] 推送群 {gid} 失败: {e}")
        return ok

    # ================= 命令 =================

    @filter.command("bili评论", alias={"bili评", "b站评论", "b站评"})
    async def cmd_comment(self, event: AstrMessageEvent, raw: str = ""):
        """看这个 B站视频并评论：<BV号 或 视频链接>"""
        if not self._c("enabled", True):
            yield event.plain_result("B站陪看评论已停用")
            return
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        bvid = parse_bvid(msg)
        if not bvid:
            yield event.plain_result("用法：bili评论 <BV号 或 视频链接>\n例：bili评论 BV1uv411q7Mv")
            return

        # 冷却检查
        if self._store:
            vc_hours = float(self._c("cooldown_hours", 24) or 0)
            left = self._store.in_video_cooldown(bvid, vc_hours)
            if left > 0:
                yield event.plain_result(f"这个视频 {left / 3600:.1f} 小时后再评吧（同视频冷却）")
                return
            gc_min = float(self._c("global_cooldown_minutes", 30) or 0)
            gleft = self._store.in_global_cooldown(gc_min)
            if gleft > 0:
                yield event.plain_result(f"刚评过，{gleft / 60:.0f} 分钟后再来（全局间隔）")
                return

        if self._lock.locked():
            yield event.plain_result("正在看上一个视频，等一下再发")
            return

        async with self._lock:
            yield event.plain_result(f"正在看 {bvid}，稍等…（下载+抽帧+理解，可能要一两分钟）")
            try:
                vc = await fetch_video(bvid, int(self._c("frame_count", 6) or 6))
            except Exception as e:
                yield event.plain_result(f"视频信息拿不到：{e}")
                return

            try:
                comment_text = await generate_comment(
                    vc, str(self._c("comment_style", "") or ""), self.context, self.cfg
                )
            finally:
                frames_for_shot = list(vc.frames)
                cover, title = vc.cover, vc.title
                owner, stat = vc.owner, vc.stat
                top_cmts = list(vc.top_comments)

            if not comment_text:
                cleanup(vc)
                yield event.plain_result("评论没生成出来（模型没返回？），这次不发了")
                return

            # 发评论（需要登录）
            sessdata = str(self._c("sessdata", "") or "").strip()
            bili_jct = str(self._c("bili_jct", "") or "").strip()
            sent = False
            err = ""
            if not (sessdata and bili_jct):
                err = "还没登录（缺 SESSDATA/bili_jct），先 `bili登录`"
            else:
                try:
                    await send_comment(vc.aid, comment_text, sessdata, bili_jct)
                    sent = True
                except Exception as e:
                    err = f"发评论失败: {e}"

            # 冷却标记（只有真正发出去才标记）
            if sent and self._store:
                self._store.mark(bvid)

            # 截图
            shot = await compose_screenshot(
                cover_url=cover,
                frames=frames_for_shot,
                title=title,
                owner=owner,
                bvid=bvid,
                comment=comment_text,
                stat=stat,
                top_comments=top_cmts,
            )
            status = "已发出评论" if sent else f"评论未发出（{err}）"
            chain = MessageChain()
            if shot and Path(shot).exists():
                chain.file_image(shot)
            chain.message(
                f"《{title}》\n{status}\n评论：{comment_text}\nhttps://www.bilibili.com/video/{bvid}"
            )
            pushed = await self._push_groups(event, chain)

            tail = f"截图已推送到群：{'、'.join(pushed)}" if pushed else "没配推送群（notify_groups），截图发下面"
            yield event.plain_result(f"《{title}》\n{status}\n评论：{comment_text}\n{tail}")
            if not pushed and shot and Path(shot).exists():
                yield MessageChain().file_image(shot)

            cleanup(vc)
            # 截图延迟删（避免消息还没发出去文件就没了）
            if shot:
                def _del_later(p=shot):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    asyncio.get_event_loop().call_later(120, _del_later)
                except Exception:
                    pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili登录", alias={"b站登录"})
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录 B站（仅管理员）。发二维码，扫完发 `bili确认`。"""
        gen = await generate_qrcode()
        if not gen or not gen[0]:
            yield event.plain_result("生成二维码失败，稍后再试")
            return
        qr_url, key = gen
        png = make_qrcode_png(qr_url)
        self._qr[str(time.time())] = {"key": key}
        chain = MessageChain().message("用 B站 App 扫码，扫完发：bili确认\n（几分钟内有效）")
        if png and Path(png).exists():
            chain.file_image(png)
        yield chain

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili确认", alias={"b站确认"})
    async def cmd_confirm(self, event: AstrMessageEvent):
        """确认扫码登录。"""
        if not self._qr:
            yield event.plain_result("没有待确认的登录，先 `bili登录`")
            return
        key = list(self._qr.values())[-1]["key"]
        res = await poll_qrcode(key, timeout=180)
        self._qr.clear()
        if not res["ok"]:
            yield event.plain_result(f"登录没成功：{res['info']}，再 `bili登录` 试一次")
            return
        ck = cookies_to_strings(res["cookies"])
        if not ck.get("SESSDATA"):
            yield event.plain_result("拿到登录信息但没有 SESSDATA，可能没登录成功")
            return
        self.cfg["sessdata"] = ck["SESSDATA"]
        self.cfg["bili_jct"] = ck.get("bili_jct", "")
        self.cfg.save_config()
        yield event.plain_result("✅ 登录成功，SESSDATA/bili_jct 已保存。现在 `bili评论` 就能真发评论了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili状态", alias={"b站状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看登录/冷却/配置状态。"""
        sess = bool(str(self._c("sessdata", "") or "").strip())
        jct = bool(str(self._c("bili_jct", "") or "").strip())
        groups = self._c("notify_groups", [])
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.replace("，", ",").split(",") if g.strip()]
        vpid = str(self._c("vision_provider_id", "") or "").strip() or "<当前>"
        vmodel = str(self._c("vision_model", "") or "").strip() or "<默认>"
        fb = self._c("vision_fallback_provider_ids", [])
        if isinstance(fb, str):
            fb = [f.strip() for f in fb.replace("，", ",").split(",") if f.strip()]
        lines = [
            "B站陪看评论状态",
            f"登录：{'已登录' if (sess and jct) else ('半登录(缺jct)' if sess else '未登录')}",
            f"视觉模型：{vpid} / {vmodel}",
            f"回退模型：{('、'.join(str(g) for g in fb) if fb else '无')}",
            f"抽帧：{self._c('frame_count', 6)} 张，超时 {self._c('vision_timeout_seconds', 120)}s",
            f"同视频冷却：{self._c('cooldown_hours', 24)} 小时，全局间隔：{self._c('global_cooldown_minutes', 30)} 分钟",
            f"推送群：{('、'.join(str(g) for g in groups) if groups else '未配置（截图只发当前会话）')}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili重置冷却", alias={"b站重置冷却"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """清空所有评论冷却记录（仅管理员）。"""
        if self._store:
            self._store._data = {"per_video": {}, "last_global": 0.0}
            self._store._save()
            yield event.plain_result("冷却记录已清空")
        else:
            yield event.plain_result("冷却存储不可用")
