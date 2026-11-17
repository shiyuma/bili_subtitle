#!/usr/bin/env python3
"""Optional local ASR fallback for Bilibili subtitle summaries."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import wave

if __package__:
    from . import bili_subtitles as bili
else:
    import bili_subtitles as bili
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

MODELS = {
    'qwen': {
        'package': 'mlx-qwen3-asr==0.4.4',
        'recognition': ('mlx-community/Qwen3-ASR-0.6B-4bit', '313d850'),
        'alignment': ('mlx-community/Qwen3-ForcedAligner-0.6B-4bit', '2f652af'),
    },
    'whisper': {'package': 'pywhispercpp==1.5.1', 'file': 'ggml-large-v3-turbo-q5_0.bin'},
}
FALLBACK_STATUSES = {'no_tracks_observed', 'no_eligible_candidate', 'needs_content_review'}
AUDIO_LIMIT = 256 * 1024 * 1024
MAX_DURATION = 4 * 60 * 60
SAMPLE_RATE = 16000
QWEN_FILES = ('config.json', 'merges.txt', 'tokenizer_config.json', 'vocab.json', 'model.safetensors')


def cache_path():
    return (Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
            / 'bilibili-subtitle-summary' / 'asr')


def preference_path():
    return bili.login_path().with_name('asr-engine')


def runner_path(cache, engine):
    return (cache / engine / 'venv' /
            ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))


def model_paths(cache, engine):
    base = cache / engine
    return [base / name for name in ('recognition', 'alignment')] if engine == 'qwen' else [base / MODELS[engine]['file']]


def available_engines():
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        return ['qwen', 'whisper']
    return ['whisper']


def configured_engine(cache):
    try:
        engine = preference_path().read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return None
    if engine not in available_engines():
        return None
    return engine if models_ready(cache, engine) and runner_path(cache, engine).is_file() else None


def models_ready(cache, engine):
    paths = model_paths(cache, engine)
    files = [p / name for p in paths for name in QWEN_FILES] if engine == 'qwen' else paths
    return all(f.is_file() and f.stat().st_size > 0 for f in files)


def worker_env(*, offline):
    env = {k: v for k, v in os.environ.items() if k not in {'BILIBILI_SESSDATA', 'PYTHONPATH'}}
    env.update(HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1')
    if offline:
        env['HF_HUB_OFFLINE'] = '1'
    return env


def run_worker(argv, *, failure, offline=True):
    try:
        subprocess.run(argv, check=True, env=worker_env(offline=offline))
    except (OSError, subprocess.CalledProcessError):
        raise bili.Problem(failure) from None


def download_models(engine, cache):
    from huggingface_hub import hf_hub_download, snapshot_download

    paths = model_paths(cache, engine)
    if engine == 'qwen':
        for key, target in zip(('recognition', 'alignment'), paths):
            repo, revision = MODELS[engine][key]
            snapshot_download(repo, revision=revision, local_dir=target, token=False,
                              allow_patterns=['*.json', '*.txt', 'model.safetensors'])
    else:
        hf_hub_download('ggerganov/whisper.cpp', MODELS[engine]['file'], revision='fb5ed93',
                        local_dir=paths[0].parent, token=False)


def initialise(engine, cache):
    if sys.version_info < (3, 10):
        raise bili.Problem('asr_requires_python_3_10_or_later')
    if engine not in available_engines():
        raise bili.Problem('asr_qwen_requires_native_apple_silicon_python')
    runner = runner_path(cache, engine)
    runner.parent.parent.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not runner.is_file():
        venv.EnvBuilder(with_pip=True).create(runner.parent.parent)
    print(f'正在准备 {engine} 本地运行环境和模型。', file=sys.stderr, flush=True)
    run_worker([str(runner), '-I', '-m', 'pip', 'install', '--disable-pip-version-check',
                '--only-binary=:all:', 'huggingface-hub>=0.34,<3', 'imageio-ffmpeg==0.8.1',
                MODELS[engine]['package']], failure='asr_dependency_install_failed', offline=False)
    if not models_ready(cache, engine):
        run_worker([str(runner), '-B', __file__, '_download', engine], failure='asr_model_download_failed', offline=False)
    if not models_ready(cache, engine):
        raise bili.Problem('asr_model_files_missing')
    module = 'mlx_qwen3_asr' if engine == 'qwen' else 'pywhispercpp.model'
    run_worker([str(runner), '-I', '-c', f'import {module}; import imageio_ffmpeg'],
               failure='asr_runtime_unavailable')
    target = preference_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, mode='w', delete=False, encoding='ascii') as handle:
        pending = Path(handle.name)
        handle.write(engine + '\n')
    try:
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)
    print(f'本地 ASR 已初始化：{engine}。')


def select_page(report_path, page, rejected):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    bvid = report.get('bvid')
    if not isinstance(bvid, str) or not re.fullmatch(bili.BV, bvid):
        raise bili.Problem('invalid_asr_report_bvid')
    target = next((p for p in report['pages'] if p['page'] == page), None)
    if (not target or target['status'] not in FALLBACK_STATUSES or target.get('stop_reason')
            or any(t.get('status') == 'download_error' and t.get('download_error') in
                   {'http_401', 'http_403', 'http_412', 'http_429'} for t in target.get('tracks', []))
            or target['status'] == 'needs_content_review' and not rejected):
        raise bili.Problem('asr_page_not_eligible: resolve fetch errors or review candidates first')
    if not bili.positive_int(target.get('cid')) or not 0 < (bili.number(target.get('duration')) or 0) <= MAX_DURATION:
        raise bili.Problem('invalid_asr_page_metadata')
    return bvid, target


def audio_url(row):
    url = row.get('baseUrl') or row.get('base_url')
    if not isinstance(url, str):
        return None
    host = urlsplit(url).hostname or ''
    if not (host.endswith(('.bilivideo.com', '.bilivideo.cn', '.hdslb.com'))):
        return None
    bili.https_parts(url, {host})
    return url


def fetch_audio(bvid, target, path):
    client = bili.Client(bili.load_login(None)[0])
    current = bili.video_pages(client.metadata(bvid)).get(target['page'])
    if not current or (current['cid'], current['duration']) != (target['cid'], target['duration']):
        raise bili.Problem('asr_video_changed: fetch subtitles again for current page')
    play = client.api('/x/player/playurl', {'bvid': bvid, 'cid': current['cid'], 'fnval': 16, 'fnver': 0})
    if play.get('is_preview') or play.get('isPreview'):
        raise bili.Problem('asr_full_audio_unavailable')
    dash = play.get('dash')
    audio = dash.get('audio') if isinstance(dash, dict) else None
    if not isinstance(audio, list):
        raise bili.Problem('asr_no_accessible_audio_track: supply a local audio file')
    tracks = []
    for row in audio:
        if not isinstance(row, dict) or (row.get('mimeType') or row.get('mime_type')) != 'audio/mp4':
            continue
        try:
            url = audio_url(row)
        except (bili.Problem, ValueError):
            continue
        if url:
            tracks.append((bili.number(row.get('bandwidth')) or 0, url))
    if not tracks:
        raise bili.Problem('asr_no_supported_audio_track: supply a local audio file')
    # Only a known HTTPS CDN, no redirect, no authorization headers or cookies.
    url = min(tracks)[1]
    try:
        request = Request(url, headers={'Referer': bili.HOME_URL, 'User-Agent': 'Mozilla/5.0'})
        with client.opener.open(request, timeout=15) as response, path.open('xb') as out:
            remaining = AUDIO_LIMIT
            while data := response.read(min(remaining + 1, 256 * 1024)):
                remaining -= len(data)
                if remaining < 0:
                    raise bili.Problem('asr_audio_exceeds_size_limit')
                out.write(data)
    except HTTPError as exc:
        with exc:
            raise bili.Problem(f'asr_audio_http_{exc.code}') from None
    except (URLError, OSError, ValueError) as exc:
        raise bili.Problem('asr_audio_download_failed') from exc


def normalise(source, wav):
    import imageio_ffmpeg

    # Explicitly allow only local files and ordinary media demuxers, not playlists.
    ffmpeg = shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist',
                             'file,pipe', '-format_whitelist', 'mov,mp4,m4a,3gp,3g2,mj2,mp3,wav,flac,ogg,matroska,webm,aac,aiff',
                             '-i', str(source), '-map', '0:a:0', '-ac', '1', '-ar', str(SAMPLE_RATE),
                             '-c:a', 'pcm_s16le', '-t', str(MAX_DURATION + 1), '-n', str(wav)],
                            stderr=subprocess.DEVNULL)
    if result.returncode:
        raise bili.Problem('asr_audio_decode_failed')
    with wave.open(str(wav), 'rb') as audio:
        duration = audio.getnframes() / audio.getframerate()
    if not 0 < duration <= MAX_DURATION:
        raise bili.Problem('asr_audio_duration_out_of_range')
    return duration


def transcribe(engine, cache, source, wav, expected=None):
    duration = normalise(source, wav)
    if expected is not None and abs(duration - expected) > max(2, expected * .01):
        raise bili.Problem('asr_audio_incomplete_or_wrong_page')
    paths = model_paths(cache, engine)
    if engine == 'qwen':
        from mlx_qwen3_asr import transcribe as qwen_transcribe
        from mlx_qwen3_asr.writers import group_subtitle_segments

        result = qwen_transcribe(str(wav), model=str(paths[0]), forced_aligner=str(paths[1]),
                                 return_timestamps=True)
        if result.truncated:
            raise bili.Problem('asr_transcript_truncated')
        grouped = group_subtitle_segments(result.segments or [], language=result.language, text=result.text)
        rows = [{'from': s['start'], 'to': s['end'], 'content': s['text']}
                for s in grouped if s['end'] > s['start']]
    else:
        from pywhispercpp.model import Model

        model = Model(str(paths[0]), print_realtime=False, print_progress=False, language='auto')
        rows = [{'from': s.t0 / 100, 'to': s.t1 / 100, 'content': s.text}
                for s in model.transcribe(str(wav)) if s.t1 > s.t0]
    cues = bili.parse_cues(rows)
    if max(cue['to'] for cue in cues) > duration + .5:
        raise bili.Problem('asr_timestamps_outside_audio')
    return {'source': 'local_asr', 'engine': engine, 'audio_duration': duration, 'body': cues}


def create_transcript(args, cache):
    engine = configured_engine(cache)
    if engine is None:
        raise bili.Problem('asr_not_initialized: run status to see choices, then init')
    if args.out.exists():
        raise bili.Problem('asr_output_already_exists')
    meta, expected = {}, None
    if args.report:
        if not args.page:
            raise bili.Problem('asr_report_requires_page')
        bvid, target = select_page(args.report, args.page, args.rejected_candidates)
        meta = {'bvid': bvid, **{k: target[k] for k in ('page', 'cid', 'duration', 'part')}}
        expected = target['duration']
    elif args.page or args.rejected_candidates:
        raise bili.Problem('asr_page_and_candidate_flags_require_report')
    with tempfile.TemporaryDirectory(prefix='bili-asr-') as temp:
        work = Path(temp)
        if args.audio:
            source = args.audio.resolve(strict=True)
            if not source.is_file() or source.stat().st_size > AUDIO_LIMIT:
                raise bili.Problem('asr_local_audio_invalid_or_too_large')
        elif args.report:
            source = work / 'source.m4a'
            fetch_audio(bvid, target, source)
        else:
            raise bili.Problem('asr_supply_report_or_local_audio')
        staging = work / 'captions.json'
        run_worker([str(runner_path(cache, engine)), '-B', __file__, '_transcribe', engine, str(source),
                    str(work / 'converted.wav'), str(staging), str(expected or 0)], failure='asr_transcription_failed')
        payload = json.loads(staging.read_text(encoding='utf-8'))
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({**meta, **payload}, handle, ensure_ascii=False, allow_nan=False)
            handle.write('\n')
    print(args.out.absolute())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    for command in ('init', '_download', '_transcribe'):
        child = sub.add_parser(command)
        child.add_argument('engine', choices=MODELS)
        if command == '_transcribe':
            for arg in ('source', 'wav', 'out'):
                child.add_argument(arg, type=Path)
            child.add_argument('expected', type=float)
    run = sub.add_parser('transcribe')
    run.add_argument('--report', type=Path)
    run.add_argument('--page', type=int)
    run.add_argument('--rejected-candidates', action='store_true')
    run.add_argument('--audio', type=Path)
    run.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    cache = cache_path()
    try:
        if args.command == 'status':
            print(json.dumps({'selected': configured_engine(cache), 'options': available_engines()}))
        elif args.command == 'init':
            initialise(args.engine, cache)
        elif args.command == '_download':
            download_models(args.engine, cache)
        elif args.command == '_transcribe':
            bili.write_json(args.out, transcribe(args.engine, cache, args.source, args.wav, args.expected or None))
        else:
            create_transcript(args, cache)
    except bili.Problem as exc:
        print(f'bili-local-asr: {exc}', file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f'bili-local-asr: operation_failed ({type(exc).__name__})', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
