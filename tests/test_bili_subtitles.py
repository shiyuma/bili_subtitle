import contextlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from scripts import bili_subtitles as bili


BVID = "BV0000000000"
CDN = "aisubtitle.hdslb.com"


def track(path, language="ai-zh", token="a"):
    return {"lan": language, "lan_doc": language,
            "subtitle_url": f"//{CDN}/bfs/{path}?auth_key={token}"}


def transcript(end, text="离线示例"):
    return {"body": [
        {"from": end / 2, "to": end / 2 + 1, "content": f"{text}中段"},
        {"from": 1.234, "to": 3.4, "content": f"{text}开头"},
        {"from": end - 2, "to": end, "content": f"{text}结尾"},
    ]}


def ok(data):
    return {"code": 0, "data": data}


class FakeHTTP:
    """Mock the HTTPS transport while exercising real URL building and JSON parsing."""

    def __init__(self, *, rounds=None, downloads=None, login_gate=False, nav=None):
        self.rounds = rounds or {}
        self.downloads = downloads or {}
        self.login_gate = login_gate
        self.nav = nav
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append(request)
        parts = urlsplit(request.full_url)
        query = parse_qs(parts.query)
        if parts.hostname == "b23.tv":
            headers = Message()
            headers["Location"] = f"https://www.bilibili.com/video/{BVID}/?p=2"
            raise HTTPError(request.full_url, 302, "", headers, io.BytesIO())
        if request.full_url == bili.HOME_URL:
            result = io.BytesIO()
            result.headers = Message()
            result.headers.add_header("Set-Cookie", "buvid3=fake-device; Path=/")
            return result
        if parts.path == "/x/web-interface/view":
            value = ok({"bvid": BVID, "title": "园艺实验", "cid": 101, "duration": 300, "pages": [
                {"page": 1, "cid": 101, "part": "土壤", "duration": 100},
                {"page": 2, "cid": 202, "part": "浇水", "duration": 200},
            ]})
        elif parts.path == "/x/web-interface/nav":
            if self.nav is None:
                raise AssertionError("an ordinary subtitle result must not probe account status")
            value = self.nav
        elif parts.path in {"/x/player/v2", "/x/player/wbi/v2"}:
            rows = self.rounds[int(query["cid"][0])].pop(0)
            value = {"code": rows} if isinstance(rows, int) else ok({
                "need_login_subtitle": self.login_gate, "subtitle": {"subtitles": rows}})
        else:
            value = self.downloads[(parts.path.rsplit("/", 1)[-1], query["auth_key"][0])]
            if isinstance(value, int):
                raise HTTPError(request.full_url, value, "", Message(), io.BytesIO())
        return io.BytesIO(json.dumps(value).encode())


class SkillTest(unittest.TestCase):
    def cli_fetch(self, transport, *args, cookie=""):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(temp)
            output = path / "result"
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(path), "BILIBILI_SESSDATA": cookie}), \
                 patch.object(bili, "build_opener", return_value=transport), patch.object(bili.time, "sleep"):
                code = bili.main(["fetch", *args, "--out", str(output)])
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            candidates = {}
            rendered = {}
            for candidate in output.glob("candidates/*/*.json"):
                candidates[candidate.stem] = json.loads(candidate.read_text(encoding="utf-8"))
                with contextlib.redirect_stdout(io.StringIO()) as buffer:
                    self.assertEqual(bili.main(["render", str(candidate), "--format", "srt"]), 0)
                rendered[candidate.stem] = buffer.getvalue()
            return code, report, candidates, rendered

    def test_late_tracks_same_language_and_per_page_duration(self):
        fake = FakeHTTP(rounds={
            101: [[], [track("short"), track("over"), track("same-duration", token="a")]]
                 + [[track("same-duration", token="rotated")]] * 5
                 + [[track("actual", token="expired"), track("english", "ai-en")],
                    [track("actual", token="fresh")]] + [[]] * 3,
            202: [[track("second")]] + [[]] * 11,
        }, downloads={
            ("short", "a"): transcript(40),
            ("over", "a"): transcript(119.5),
            ("same-duration", "a"): transcript(100, "汽车测评串台"),
            ("actual", "expired"): 403,
            ("actual", "fresh"): transcript(99, "园艺土壤"),
            ("english", "a"): transcript(100, "Soil"),
            ("second", "a"): transcript(200, "园艺浇水"),
        })
        code, report, candidates, rendered = self.cli_fetch(
            fake, BVID, "--all-pages", "--lang", "ai-zh", "--fallback-language", cookie="fake-sessdata")
        self.assertEqual(code, 0)
        self.assertEqual([(p["cid"], p["duration"]) for p in report["pages"]], [(101, 100), (202, 200)])
        p1, p2 = report["pages"]
        self.assertEqual(p1["rounds_attempted"], 12)
        self.assertEqual(p2["rounds_attempted"], 12)
        self.assertEqual(len(p1["candidate_order"]), 3)
        ranked = [candidates[key]["body"][0]["content"] for key in p1["candidate_order"]]
        self.assertEqual(ranked, ["汽车测评串台开头", "园艺土壤开头", "Soil开头"])
        self.assertEqual(sorted(t["coverage"] for t in p1["tracks"] if t["status"] == "coverage_mismatch"),
                         [0.4, 1.195])
        self.assertIn("00:00:01,234 --> 00:00:03,400", rendered[p1["candidate_order"][1]])
        self.assertIn("00:03:18,000 --> 00:03:20,000", rendered[p2["candidate_order"][0]])
        cdn_calls = [r for r in fake.calls if r.host == CDN]
        self.assertEqual(len(cdn_calls), 7)
        self.assertTrue(all(r.get_header("Cookie") is None for r in cdn_calls))
        api_calls = [r for r in fake.calls if r.host == "api.bilibili.com"]
        self.assertTrue(all(r.get_header("Cookie") == "SESSDATA=fake-sessdata; buvid3=fake-device"
                            for r in api_calls))
        self.assertNotIn("auth_key", json.dumps(report))
        self.assertNotIn("fake-sessdata", json.dumps(report))

    def test_short_link_page_and_strict_language(self):
        fake = FakeHTTP(rounds={202: [[track("foreign", "ai-en")]]})
        code, report, candidates, _ = self.cli_fetch(fake, "https://b23.tv/example", "--rounds", "1",
                                                     "--lang", "ai-zh")
        self.assertEqual(code, 2)
        self.assertEqual(report["requested_pages"], [2])
        self.assertEqual(report["pages"][0]["tracks"][0]["status"], "other_language")
        self.assertFalse(candidates)
        self.assertIsNone(fake.calls[0].get_header("Cookie"))

    def test_anon_login_gate_is_not_no_subtitles(self):
        fake = FakeHTTP(rounds={101: [[]]}, login_gate=True)
        code, report, _, _ = self.cli_fetch(fake, BVID)
        page = report["pages"][0]
        self.assertEqual(code, 2)
        self.assertEqual(page["status"], "login_required_or_expired")
        self.assertEqual(page["rounds_attempted"], 1)
        self.assertEqual(report["login_source"], "anonymous")

    def test_player_login_gate_checks_account_before_recommending_rescan(self):
        for player_response, nav, check, expected in (
                ([], ok({"isLogin": True}), "valid", "access_unavailable_with_valid_login"),
                (-101, {"code": -101}, "expired", "login_required_or_expired"),
                ([], {"code": -352}, "unverified", "login_status_unverified")):
            with self.subTest(check=check):
                fake = FakeHTTP(rounds={101: [player_response]}, login_gate=True, nav=nav)
                code, report, _, _ = self.cli_fetch(fake, BVID, "--rounds", "1", cookie="fake-session")
                self.assertEqual(code, 2)
                self.assertEqual(report["login_check"], check)
                self.assertEqual(report["pages"][0]["status"], expected)
                calls = [r for r in fake.calls if "/x/web-interface/nav" in r.full_url]
                self.assertEqual(len(calls), 1)
                self.assertIn("SESSDATA=fake-session", calls[0].get_header("Cookie"))

    def test_empty_subtitles_with_saved_login_do_not_signal_expiry(self):
        fake = FakeHTTP(rounds={101: [[]]})
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), \
             patch.dict(os.environ, {"XDG_CONFIG_HOME": temp, "BILIBILI_SESSDATA": ""}), \
             patch.object(bili, "build_opener", return_value=fake):
            bili.store_sessdata(bili.login_path(), "fake-session")
            output = Path(temp) / "result"
            self.assertEqual(bili.main(["fetch", BVID, "--rounds", "1", "--out", str(output)]), 2)
            self.assertTrue(bili.login_path().is_file())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["login_source"], "saved")
            self.assertNotIn("login_check", report)
            self.assertEqual(report["pages"][0]["status"], "no_tracks_observed")

    def test_risk_control_stops_remaining_rounds_and_pages(self):
        fake = FakeHTTP(rounds={101: [[], -352]})
        code, report, _, _ = self.cli_fetch(fake, BVID, "--all-pages")
        self.assertEqual(code, 2)
        first, second = report["pages"]
        self.assertEqual((first["status"], first["rounds_attempted"]), ("request_stopped", 2))
        self.assertEqual(second["status"], "not_attempted")
        self.assertEqual(second["stop_reason"], "api_-352")

    def test_coverage_boundary_and_malformed_cues(self):
        for end, accepted in [(94.99999, False), (95, True), (105, True), (105.00001, False)]:
            with self.subTest(end=end):
                self.assertEqual(bili.metrics(bili.parse_cues(transcript(end)["body"]), 100)[1], accepted)
        for cue in [{"from": 4, "to": 1, "content": "bad"},
                    {"from": 1, "to": float("nan"), "content": "bad"}]:
            with self.assertRaises(bili.Problem):
                bili.parse_cues([cue])

    def test_reject_unsafe_cdn_address_and_cookie_file(self):
        for url in ("https://aisubtitle.hdslb.com.evil.example/cue",
                    "https://evil.example@aisubtitle.hdslb.com/cue",
                    "http://aisubtitle.hdslb.com/cue"):
            with self.assertRaises(bili.Problem):
                bili.subtitle_location(url)
        if os.name == "posix":
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "cookie"
                path.write_text("test-value%2C0", encoding="utf-8")
                path.chmod(0o644)
                with self.assertRaisesRegex(bili.Problem, "file_permissions"):
                    bili.load_login(path)
                path.chmod(0o600)
                self.assertEqual(bili.load_login(path), ("test-value%2C0", "file"))


if __name__ == "__main__":
    unittest.main()
