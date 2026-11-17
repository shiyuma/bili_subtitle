# 本地转写

只在字幕报告为 `no_tracks_observed`、`no_eligible_candidate`，或已实际核对并拒绝该 P 全部候选时使用。遇到登录、访问拒绝、限流或网络失败，先按实际原因处理；不把这些状态当作无字幕。多分 P 只转写缺少可靠字幕的分 P。若用户已经提供该 P 的完整本机音频，可用它代替获取播放音轨。

```bash
python3 scripts/local_asr.py status
```

`selected` 有值就复用；为空时，在对话里说明当前检测到的平台与 Python 版本、需要下载模型和安装独立环境依赖、预计模型大小及本地保存位置，让用户明确选择一种或跳过。等待回复；预选项、沉默或超时不代表同意。用户本会话已经明确选择并授权安装时无需重复确认：

- Apple Silicon：Qwen3-ASR 0.6B（4 位识别和时间戳对齐模型，合计约 1.69 GB）。[识别模型](https://huggingface.co/mlx-community/Qwen3-ASR-0.6B-4bit/tree/main) · [对齐模型](https://huggingface.co/mlx-community/Qwen3-ForcedAligner-0.6B-4bit/tree/main)
- Whisper large-v3-turbo（Q5 量化，约 574 MB），使用 `pywhispercpp` 的预编译包；该选项尚未完成真实模型端到端验证，视为实验功能。 [模型](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-large-v3-turbo-q5_0.bin) · [运行库](https://pypi.org/project/pywhispercpp/)

选定后执行以下命令之一，需要 Python 3.10+、所选运行库有对应平台的预编译包，以及首次联网下载。脚本在独立缓存环境安装对应 Python 依赖及 FFmpeg，不修改系统 Python；依赖下载大小另计，实际用量因平台而异。Qwen 需要原生 ARM64 Python 和可用的 Metal GPU；若沙箱内无法访问 GPU，按运行环境的权限流程执行。无需安装系统级 FFmpeg。

```bash
python3 scripts/local_asr.py init qwen
python3 scripts/local_asr.py init whisper
```

模型及独立环境默认保存在 `~/.cache/bilibili-subtitle-summary/asr`，可用 `XDG_CACHE_HOME` 更改；选项记在 `~/.config/bilibili-subtitle-summary/asr-engine`，与 B 站凭据分开。更换模型时显式运行另一个 `init`，已有模型可继续保留在缓存里。初始化失败不覆盖原来的选择。

用当前 `fetch` 生成的私有报告指定分 P。只有已经核对并拒绝全部候选后，才传 `--rejected-candidates`。临时音频仅取该 P 的播放音轨，完成后自动清理；脚本不获取视频画面，也不把音频发给模型服务。

```bash
python3 scripts/local_asr.py transcribe --report "$run_dir/result/report.json" --page 1 --out "$run_dir/P1-asr.json"
python3 scripts/bili_subtitles.py render "$run_dir/P1-asr.json" --format timed
```

如果 B 站未提供可访问的完整音轨，报告原因并请用户提供合法可用的本地音频。可在上一条命令加 `--audio /path/to/audio.wav`；也可只传 `--audio` 和 `--out` 独立转写。本地音频不被删除。生成的 JSON 和 SRT 与模型权重一样都不提交到仓库。

播放音轨会重新核对分 P 和时长；转写时间戳不得超出音频。最后一条人声可能早于片尾，因此不套用 B 站原有字幕的 95% 校验。检查开头、中段、结尾和异常重复或缺段，有疑问就对照原声；总结或导出时注明「本地 ASR」，不要声称是人工字幕或精确引语。
