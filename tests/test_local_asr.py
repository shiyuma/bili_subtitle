import io
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import bili_subtitles as bili
from scripts import local_asr as asr


class LocalASRTests(unittest.TestCase):
    def test_uninitialized_status_and_transcription_never_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(asr, 'cache_path', return_value=root / 'cache'), \
                 patch.object(asr, 'preference_path', return_value=root / 'preference'), \
                 patch.object(asr, 'initialise') as init, \
                 patch.object(asr, 'download_models') as download, \
                 contextlib.redirect_stdout(io.StringIO()) as output, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(asr.main(['status']), 0)
                self.assertIsNone(json.loads(output.getvalue())['selected'])
                self.assertEqual(asr.main(['transcribe', '--audio', str(root / 'test.wav'),
                                           '--out', str(root / 'result.json')]), 1)
                init.assert_not_called()
                download.assert_not_called()
                self.assertFalse((root / 'result.json').exists())

    def test_fallback_is_per_page_and_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            for status, stop, rejected, expected in [
                ('no_tracks_observed', None, False, True),
                ('no_eligible_candidate', None, False, True),
                ('needs_content_review', None, False, False),
                ('needs_content_review', None, True, True),
                ('needs_content_review', 'api_-412', True, False),
                ('login_required_or_expired', None, True, False),
                ('request_failed', None, True, False),
            ]:
                with self.subTest(status=status, stop=stop, rejected=rejected):
                    page = {'page': 2, 'cid': 9, 'duration': 25, 'status': status, 'stop_reason': stop}
                    report.write_text(json.dumps({'bvid': 'BV0000000000', 'pages': [page]}))
                    if expected:
                        self.assertEqual(asr.select_page(report, 2, rejected), ('BV0000000000', page))
                    else:
                        with self.assertRaisesRegex(bili.Problem, 'not_eligible'):
                            asr.select_page(report, 2, rejected)

    def test_only_audio_for_matching_cid_and_no_cookie_to_cdn(self):
        class FakeClient:
            requests = []
            opener = None

            def __init__(self, state):
                self.state = state
                self.opener = self

            def metadata(self, bvid):
                return {'pages': [{'page': 2, 'cid': 9, 'duration': 25}]}

            def api(self, name, query):
                self.requests.append((name, query))
                return {'dash': {'audio': [
                    {'mimeType': 'audio/mp4', 'baseUrl': 'https://bilivideo.com.example/a', 'bandwidth': 1},
                    {'mimeType': 'audio/mp4', 'baseUrl': 'https://audio.bilivideo.com/a?signature=ok', 'bandwidth': 60},
                    {'mimeType': 'audio/mp4', 'baseUrl': 'https://audio.bilivideo.com/b', 'bandwidth': 100},
                ]}}

            def open(self, request, timeout):
                self.requests.append(request)
                return io.BytesIO(b'synthetic audio bytes')

        with tempfile.TemporaryDirectory() as directory, patch.object(bili, 'Client', FakeClient), \
             patch.object(bili, 'load_login', return_value=('fake-credential', 'saved')):
            dest = Path(directory) / 'audio.m4a'
            with self.assertRaisesRegex(bili.Problem, 'video_changed'):
                asr.fetch_audio('BV0000000000', {'page': 2, 'cid': 1, 'duration': 25}, dest)
            self.assertFalse(dest.exists())
            asr.fetch_audio('BV0000000000', {'page': 2, 'cid': 9, 'duration': 25}, dest)
            (_, query), request = FakeClient.requests
            self.assertEqual(query['cid'], 9)
            self.assertEqual(request.full_url, 'https://audio.bilivideo.com/a?signature=ok')
            self.assertIsNone(request.get_header('Cookie'))
            self.assertEqual(dest.read_bytes(), b'synthetic audio bytes')

    def test_partial_model_cache_does_not_count_as_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            choice = root / 'asr-engine'
            choice.write_text('whisper\n')
            runner = asr.runner_path(root, 'whisper')
            runner.parent.mkdir(parents=True)
            runner.touch()
            model, = asr.model_paths(root, 'whisper')
            model.with_suffix('.part').write_bytes(b'incomplete')
            with patch.object(asr, 'preference_path', return_value=choice):
                self.assertIsNone(asr.configured_engine(root))
                model.write_bytes(b'fake weights')
                self.assertEqual(asr.configured_engine(root), 'whisper')
