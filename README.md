# B站陪看评论 (astrbot_plugin_bili_watcher)

让 bot **看懂** B 站视频，然后像真人一样随手留一条评论。

不是刷量工具——**不点赞、不投币、不关注**，只偶尔评论。

## 它干嘛

发一个 BV 号或视频链接 → bot 下载视频（yt-dlp）→ ffmpeg 均匀抽 N 帧 → 喂给多模态视觉模型"看懂" → 结合标题/简介/弹幕/热门评论生成一条短评 → 用你的 B 站登录身份发出去 → 渲染一张**可爱风截图**（相框模板 + 封面 + 数据 + 热门评论 + 我的评论）推送到指定群给你看。

## 命令

| 命令 | 说明 |
|---|---|
| `bili评论 <BV号/链接>` | 看视频并评论（核心） |
| `bili登录` | 发 B 站扫码二维码（仅管理员） |
| `bili确认` | 扫码后确认，SESSDATA/bili_jct 自动存配置 |
| `bili状态` | 看登录状态 / 视觉模型 / 回退链 / 冷却 / 推送群 |
| `bili重置冷却` | 清空所有冷却记录（仅管理员） |

别名：`bili评` / `b站评论` / `b站评` / `b站登录` / `b站确认` / `b站状态` / `b站重置冷却`

## 安装依赖

```bash
pip install -r requirements.txt
```

包含：`bilibili_api`、`yt-dlp`、`aiohttp`、`Pillow`、`imageio-ffmpeg`（自带 ffmpeg 二进制）、`qrcode`

## 配置

| 项 | 说明 |
|---|---|
| `vision_provider_id` | **视觉模型提供商 ID**，填 AstrBot 里已配好的服务商（支持图片输入） |
| `vision_model` | 具体模型名，如 `gemini-2.0-flash`、`gpt-4o` |
| `vision_fallback_provider_ids` | **回退提供商列表**，主模型挂了按顺序试 |
| `vision_timeout_seconds` | 视觉调用超时，看图慢建议 120+ |
| `frame_count` | 抽帧数量，建议 4-8 |
| `cooldown_hours` | 同视频冷却（小时） |
| `global_cooldown_minutes` | 全局评论间隔（分钟） |
| `comment_style` | 评论风格提示词 |
| `notify_groups` | 截图推送群号列表 |
| `sessdata` / `bili_jct` | B 站登录凭证，`bili登录` 扫码自动填 |

视觉模型三级兜底：**主视觉模型 → 回退列表 → 当前正在用的提供商**。
所以不用把 AstrBot 主聊天模型换成多模态的——主聊天继续用 deepseek，看视频单独走 gemini。

## 自定义相框模板

`assets/template.jpg` 是截图用的可爱风相框（1024×1536），换一张同类留白的图即可，
写入安全区在 `core/screenshot.py` 顶部的 `X0, Y0, X1, Y1`。

## 说明

- 登录凭证只存本机配置文件，不上传
- 评论频率受「同视频冷却 + 全局间隔」双重限制，避免刷屏
- 视频下载到系统临时目录，评完自动清理
