import contextlib
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from scripts import bili_subtitles as bili
from test_bili_subtitles import BVID, FakeHTTP, ok, track, transcript


KEY = "1234567890abcdef1234567890abcdef"
SCAN_URL = f"https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1&qrcode_key={KEY}"
FAKE_COOKIE = "local-test-sessdata%2C0"


class FakePassport:
    def __init__(self, statuses, *, cookie=None):
        self.statuses = iter(statuses)
        self.cookie = cookie
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        parts = urlsplit(request.full_url)
        headers = Message()
        if parts.path.endswith("/generate"):
            value = ok({"url": SCAN_URL, "qrcode_key": KEY})
        else:
            if parse_qs(parts.query) != {"qrcode_key": [KEY]}:
                raise AssertionError("poll request did not reuse the generated QR session")
            status = next(self.statuses)
            value = ok({"code": status, "refresh_token": "must-not-store",
                        "url": "https://biligame.example/must-not-follow"})
            if status == 0 and self.cookie:
                headers.add_header("Set-Cookie", "bili_jct=ignored; Path=/")
                headers.add_header("Set-Cookie", f"SESSDATA={self.cookie}; Domain=.bilibili.com; Secure")
        result = io.BytesIO(json.dumps(value).encode())
        result.headers = headers
        return result


class QrLoginTest(unittest.TestCase):
    def login(self, path, passport):
        image, credential = path / "bilibili-login.png", path / "sessdata"
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr, \
             patch.object(bili, "build_opener", return_value=passport), \
             patch.object(bili.time, "sleep") as sleep, \
             patch.object(bili, "write_qr_png"), patch.dict(sys.modules, {"segno": object()}):
            result = bili.main(["qr-login", "--image", str(image), "--sessdata-file", str(credential)])
        output = stdout.getvalue() + stderr.getvalue()
        for secret in (KEY, FAKE_COOKIE, "must-not-store", "must-not-follow"):
            self.assertNotIn(secret, output)
        self.assertTrue(all(request.get_header("Cookie") is None for request in passport.requests))
        return result, output, sleep.call_count

    def test_mobile_confirmation_then_authenticated_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            passport = FakePassport([86101, 86090, 0], cookie=FAKE_COOKIE)
            result, output, waits = self.login(path, passport)
            self.assertEqual((result, waits), (0, 2))
            self.assertIn(str(path / "bilibili-login.png"), output)
            self.assertIn("已扫码，等待在手机上确认", output)
            self.assertIn("扫码登录成功", output)
            self.assertEqual(len(passport.requests), 4)
            self.assertEqual((path / "sessdata").read_text().strip(), FAKE_COOKIE)
            if os.name == "posix":
                self.assertEqual((path / "sessdata").stat().st_mode & 0o777, 0o600)
            fake = FakeHTTP(rounds={101: [[track("valid")]]}, downloads={("valid", "a"): transcript(100)})
            with contextlib.redirect_stdout(io.StringIO()), patch.object(bili, "build_opener", return_value=fake):
                fetch = bili.main(["fetch", BVID, "--rounds", "1", "--out", str(path / "result"),
                                   "--sessdata-file", str(path / "sessdata")])
            self.assertEqual(fetch, 0)
            player = [request for request in fake.calls if "/x/player/" in request.full_url]
            self.assertEqual(player[0].get_header("Cookie"), f"SESSDATA={FAKE_COOKIE}; buvid3=fake-device")
            self.assertEqual(len(list((path / "result").glob("candidates/*/*.json"))), 1)

    def test_expired_or_missing_cookie_is_not_login_success(self):
        for status, reason in ((86038, "qr_expired"), (0, "qr_confirmed_without_sessdata")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                result, output, _ = self.login(path, FakePassport([status]))
                self.assertEqual(result, 1)
                self.assertIn(reason, output)
                self.assertFalse((path / "sessdata").exists())

    def test_connection_reset_during_generation_does_not_offer_a_qr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            passport = Mock(requests=[])
            passport.open.side_effect = ConnectionResetError(54, "test connection reset")
            result, output, waits = self.login(path, passport)
            self.assertEqual((result, waits), (1, 0))
            self.assertIn("qr_generate_network_connection_reset", output)
            self.assertNotIn("bilibili-login.png", output)
            self.assertEqual(list(path.iterdir()), [])

    def test_reject_qr_link_outside_official_scan_endpoint(self):
        for url in (SCAN_URL.replace("account.bilibili.com", "example.com"),
                    SCAN_URL.replace("auth/scan-web", "auth/other"),
                    SCAN_URL.replace("account.bilibili.com", bili.PASSPORT_HOST),
                    SCAN_URL.replace(KEY, "wrong-key")):
            with self.assertRaises(bili.Problem):
                bili.qr_session(ok({"url": url, "qrcode_key": KEY}))
        legacy = f"https://{bili.PASSPORT_HOST}/h5-app/passport/login/scan?qrcode_key={KEY}"
        self.assertEqual(bili.qr_session(ok({"url": legacy, "qrcode_key": KEY})), (legacy, KEY))

    @unittest.skipUnless(importlib.util.find_spec("segno"), "optional QR dependency not installed")
    def test_render_real_png_without_overwriting(self):
        import segno

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "bilibili-login.png"
            bili.write_qr_png(segno, SCAN_URL, image)
            original = image.read_bytes()
            self.assertTrue(original.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(original), 100)
            with self.assertRaises(FileExistsError):
                bili.write_qr_png(segno, SCAN_URL, image)
            self.assertEqual(image.read_bytes(), original)
