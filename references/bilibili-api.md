# B 站字幕接口与排查

## 扫码登录

播放器接口可能返回 `need_login_subtitle=true` 和空列表。使用本人有权观看该视频的 B 站账号。首次使用扫码需在当前 Python 环境安装 `requirements-qr.txt` 中的 Segno；它在本地把 B 站返回的登录链接绘制成 PNG。

```bash
login_dir="$(mktemp -d)"
python3 -m venv "$login_dir/venv"
"$login_dir/venv/bin/python" -m pip install -r requirements-qr.txt
"$login_dir/venv/bin/python" scripts/bili_subtitles.py qr-login --image "$login_dir/bilibili-login.png" --sessdata-file "$login_dir/sessdata"
```

在 Codex 中，由助手准备依赖和临时目录，让 `qr-login` 在后台运行并保留工具的进程会话。命令会在轮询前输出 PNG 的绝对路径；拿到路径就在对话中用图片方式显示（本地 Markdown 图片用 `![B 站登录二维码](/绝对路径/bilibili-login.png)`），请用户用哔哩哔哩 App 扫码并在手机上确认。不要打开浏览器，不要打印二维码原始链接、扫码密钥或响应内容。扫码后用同一个 `--sessdata-file` 继续 `fetch`；全部抓完后删除临时凭据和二维码。二维码通常约 3 分钟失效；若超时，请重新生成，不能使用旧图继续。

脚本先 GET `https://passport.bilibili.com/x/passport-login/web/qrcode/generate`，再约每 3 秒 GET 同路径下的 `poll?qrcode_key=...`。外层 `code` 表示接口请求状态，内层 `data.code` 表示扫码状态：86101 未扫码，86090 已扫码待确认，86038 过期，0 已确认。确认后只从响应的 `Set-Cookie` 读取 GET 字幕所需的 `SESSDATA`，不访问回跳地址，不保存刷新令牌或其他 Cookie。若官方接口连接失败，不能生成可扫描的二维码。

扫码链接支持 B 站的 `account.bilibili.com/h5/account-h5/auth/scan-web` 和旧版 `passport.bilibili.com/h5-app/passport/login/scan`，并核对链接中的扫码密钥与会话一致。若 macOS 的 Python 未加载任何根证书，脚本会尝试系统的 `/etc/ssl/cert.pem`，保留 HTTPS 证书校验。若仍出现 `network_tls_error`，检查 Python 的证书配置或改用带可信 CA 的 Python。

## 手动配置登录态

已有浏览器登录态也可手动配置。在浏览器的开发者工具打开「应用 / 存储 → Cookie → https://www.bilibili.com」，复制 `SESSDATA` **这一项的值**，在本机终端运行：

```bash
python3 scripts/bili_subtitles.py login
```

粘贴到不回显的提示符。默认保存在 `~/.config/bilibili-subtitle-summary/sessdata`（支持 `XDG_CONFIG_HOME`），文件权限为 0600。重新运行可更新；删除该文件可清除本地登录态。若环境已提供 `BILIBILI_SESSDATA`，脚本优先使用该变量；也可以用 `fetch --sessdata-file /本机私有路径` 显式指定文件，覆盖变量和默认路径。不要使用命令行参数、聊天、日志或仓库存放 Cookie 值。

脚本尽力先访问主页取得 `buvid3`，没有拿到也继续。只向 `api.bilibili.com` 发送登录 Cookie；短链、主页和字幕 CDN 请求均不带登录 Cookie。

## 请求链路

1. `GET https://api.bilibili.com/x/web-interface/view?bvid=...` → 标题、简介和 `pages[]`。每个分 P 用自己的 `cid` 和 `duration`；单 P 缺少 `pages` 时才用顶层字段。
2. `GET https://api.bilibili.com/x/player/v2?bvid=...&cid=...` → `data.subtitle.subtitles[]`。请求带浏览器 UA 和 B 站 Referer。播放器返回成功但没看到轨道时，不能据此判断视频没有字幕。
3. GET 轨道的 `subtitle_url`，将开头 `//` 补成 `https://`。下载时保留原签名；只用不含查询参数的路径去重。同一 `lan` 的不同路径分别评估。同路径下载失败后，允许在后续轮次用新签名重试。读取 `body[].from/to/content`，时间单位是秒。
4. 检查时间字段和正文，按 cue 起点排序。用最后结束的有效 cue 算覆盖率。报告只保存路径指纹、语言和诊断，不记录带签名的 URL。仅覆盖率达标的正文保存为候选。

实测同一 `cid` 在不同轮次会返回不同轨道，也有字幕内容属于其他视频的情况。换源后旧转写残留是一种可能解释，不能据此推断平台机制。相近时长的错轨可能通过筛选；真轨也可能因长片尾、长静音或不完整字幕被拒。被拒轨不作为视频全文的依据；需要时让用户提供播放器里确认过的字幕或另行确认转写需求。

## 排查

- `needs_content_review`：有通过时长校验的候选，按样本和正文继续核对。请求因限流中途停止时仍可能已有候选，见 `stop_reason`。
- `login_required_or_expired`：检查本机登录态。匿名且接口明确要求登录时提前结束该 P；已配置但仍提示时，重新扫码，并确认账号有权观看该视频。
- `no_tracks_observed`：在采样窗口内没见到轨道，可能尚未生成、暂时未返回或视频没有字幕。可先在网页播放器确认。
- `no_eligible_candidate`：查看每条轨的 `status`、`coverage`（比例，1.0 为 100%）、`cue_count`、`download_error` 或 `reason`。如只有其他语言且用户允许，使用 `--fallback-language`。
- `request_stopped` / `not_attempted`：查看平台拒绝或限流状态，停止自动重试。若只是旧播放器端点不可用（例如 404）且没有限流，可另用 `--player wbi` 试 `/x/player/wbi/v2`。该选项走常见的无签名请求；如果平台要求 WBI 签名，当前脚本会报告失败，不会自动改签名、切换账号或重试绕过限制。
- `request_failed` / `no_usable_track_url`：查看 `player_errors` 或 `invalid_track_entries`，区分网络问题、响应结构变化和缺失的字幕地址。

平台使用非公开网页接口，兼容性可能变化。播放器字幕格式可参照 [yt-dlp 的 Bilibili 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)；串台现象另有[公开复现](https://github.com/XZXZZX-Ai/bilibili-mcp/issues/80)。
