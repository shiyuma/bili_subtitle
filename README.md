# B 站字幕与视频总结

一个 Codex Skill，用于提取哔哩哔哩视频现有字幕，并生成带时间戳的总结。脚本轮询字幕轨道，按分 P 时长筛掉明显错轨；Codex 再核对候选内容，避免把串台字幕当成视频正文。支持 BV 号、普通视频链接、`b23.tv` 短链和多分 P。

需要 Python 3.9+。提取和导出只用标准库，扫码登录需要额外安装 Segno。提取脚本不调用模型 API；总结由使用此 Skill 的 Codex 完成。

## 安装并使用

在终端安装到 Codex 的 Skills 目录：

```bash
skills_dir="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skills_dir"
git clone https://github.com/shiyuma/bili_subtitle.git "$skills_dir/bilibili-subtitle-summary"
```

也可以下载仓库后，将包含 `SKILL.md` 的整个目录命名为 `bilibili-subtitle-summary`，放到同一位置。在 Codex 中调用：

> 用 $bilibili-subtitle-summary 看这个视频：<视频链接>

只给链接或 BV 号时，默认直接给带时间戳的总结。Skill 会从字幕判断视频在做什么：访谈抓问题和观点，科普抓机制和证据，故事抓事件和转折，教程抓步骤和易错点。只想拿文件时，直接说「只要 SRT」或「只要纯文字稿」。

需要登录时，Codex 可以[直接在对话中显示二维码](references/bilibili-api.md#扫码登录)。用哔哩哔哩 App 扫码确认一次，同一台设备上的后续对话会复用本机登录态；B 站确认登录态失效后再扫码。

通过 Git 安装的版本可以用 `git -C "$skills_dir/bilibili-subtitle-summary" pull --ff-only` 更新（在同一个终端，或重新设置 `skills_dir`）。

## 命令行

在本目录运行：

```bash
run_dir="$(mktemp -d)"
python3 scripts/bili_subtitles.py fetch '<视频链接或 BV 号>' --out "$run_dir/result"
```

默认使用 URL 中的 `?p=N` 或 P1。指定多个 P 可用 `--page 2 --page 4`，整个视频用 `--all-pages`。每 P 默认采样 12 轮；`--rounds 3` 可快速试跑，但更容易漏掉轨道。

若返回 `login_required_or_expired`，按[扫码步骤](references/bilibili-api.md#扫码登录)登录后，直接重跑 `fetch`，换一个新的 `--out` 目录即可。

打开输出目录的 `report.json`，检查候选的首、中、尾样本及 cue 数量。确认内容后，把报告中的候选文件路径传给：

```bash
python3 scripts/bili_subtitles.py render '<候选 JSON 文件路径>' --format srt > subtitles.srt
```

也可用 `--format text` 输出纯文本，或 `--format timed` 输出带时间戳的文字稿。退出码 `0` 表示请求的每个 P 都有候选等待复核，`2` 表示至少一个 P 没有候选（仍有报告），`1` 表示输入、网络或本地执行错误。时长达标只代表候选可供复核，不能证明内容正确。

## 验证

```bash
python3 -B -m unittest discover -s tests -v
```

离线测试不使用真实账号、字幕或视频。不要提交 Cookie、带签名的 CDN URL 或本地抓取结果。
