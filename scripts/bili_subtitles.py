#!/usr/bin/env python3
"""Collect and inspect Bilibili subtitle candidates; export a reviewed candidate."""
import argparse
import getpass
import hashlib
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

HOME_URL = "https://www.bilibili.com/"
API = "https://api.bilibili.com"
PASSPORT_HOST = "passport.bilibili.com"
QR_API = f"https://{PASSPORT_HOST}/x/passport-login/web/qrcode"
QR_SCAN_PATHS = {PASSPORT_HOST: "/h5-app/passport/login/scan",
                 "account.bilibili.com": "/h5/account-h5/auth/scan-web"}
QR_TTL, QR_POLL_INTERVAL = 180, 3
LOGIN_ERRORS = {"api_-101", "http_401"}
VIDEO_HOSTS = {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
SHORT_HOST = "b23.tv"
SUBTITLE_HOSTS = {"aisubtitle.hdslb.com", "subtitle.bilibili.com", "i0.hdslb.com",
                  "i1.hdslb.com", "i2.hdslb.com"}
BV = r"BV[A-Za-z0-9]{10}"
MIN_COVERAGE, MAX_COVERAGE = 0.95, 1.05
MAX_BYTES = 16 * 1024 * 1024
COOKIE_VALUE = re.compile(r"[A-Za-z0-9%_.,+~/=:-]+")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Referer": HOME_URL,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class Problem(Exception):
    """A diagnostic that is safe to print; never include a request URL or cookie."""

    def __init__(self, reason, *, stop=False):
        super().__init__(reason)
        self.stop = stop


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def https_parts(url, allowed_hosts):
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname in allowed_hosts
                and not parts.username and parts.password is None
                and parts.port in (None, 443) and "\\" not in parts.netloc):
            return parts
    except ValueError:
        pass
    raise Problem("unexpected_url_host_or_scheme")


def subtitle_location(url):
    if not isinstance(url, str) or not url:
        raise Problem("missing_subtitle_url")
    url = "https:" + url if url.startswith("//") else url
    parts = https_parts(url, SUBTITLE_HOSTS)
    if not parts.path or parts.path == "/":
        raise Problem("missing_subtitle_path")
    # Preserve the signed query for the GET; use only the path for deduplication.
    track_id = hashlib.sha256(parts.path.encode()).hexdigest()[:16]
    return url, track_id


def login_path():
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "bilibili-subtitle-summary" / "sessdata"


def checked_cookie(value):
    if not COOKIE_VALUE.fullmatch(value) or len(value) > 4096:
        raise Problem("invalid_sessdata_value: use only the SESSDATA cookie value")
    return value


def load_login(explicit_file):
    if explicit_file is None and os.environ.get("BILIBILI_SESSDATA"):
        return checked_cookie(os.environ["BILIBILI_SESSDATA"]), "environment"
    file = explicit_file or login_path()
    try:
        if not file.is_file():
            if explicit_file is None and not file.exists():
                return "", "anonymous"
            raise Problem("sessdata_file_not_found_or_not_regular")
        with file.open(encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise Problem("sessdata_file_permissions: file must belong to you and use chmod 600")
            return checked_cookie(handle.read(4097).strip()), "file" if explicit_file else "saved"
    except (OSError, UnicodeError):
        raise Problem("cannot_read_sessdata_file") from None


def save_login(path):
    if not sys.stdin.isatty():
        raise Problem("login_requires_interactive_terminal")
    store_sessdata(path, getpass.getpass("SESSDATA（只粘贴值，输入不回显）: ").strip())
    print("已保存本地登录态。")


def store_sessdata(path, value):
    value = checked_cookie(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as f:
            temporary = Path(f.name)
            f.write(value + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def network_reason(error):
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(cause, socket.gaierror):
        return "network_dns_error"
    if isinstance(cause, ssl.SSLError):
        return "network_tls_error"
    if isinstance(cause, TimeoutError):
        return "network_timeout"
    if isinstance(cause, ConnectionResetError):
        return "network_connection_reset"
    return "network_error"


def https_context():
    context = ssl.create_default_context()
    # Some standalone macOS Python installs ship without a configured CA bundle.
    macos_bundle = Path("/etc/ssl/cert.pem")
    if (sys.platform == "darwin" and not context.cert_store_stats()["x509_ca"]
            and not any(os.environ.get(key) for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"))
            and macos_bundle.is_file()):
        context.load_verify_locations(cafile=str(macos_bundle))
    return context


class Client:
    def __init__(self, sessdata="", interval=2.0, player="legacy"):
        self.sessdata = sessdata
        self.interval = interval
        self.endpoint = "/x/player/v2" if player == "legacy" else "/x/player/wbi/v2"
        self.buvid3 = ""
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=https_context()))
        self.last_player = None

    def request(self, url, *, api_cookie=False, kind="json"):
        headers = dict(HEADERS)
        if api_cookie:
            https_parts(url, {"api.bilibili.com"})
            cookies = [f"{k}={v}" for k, v in
                       (("SESSDATA", self.sessdata), ("buvid3", self.buvid3)) if v]
            if cookies:
                headers["Cookie"] = "; ".join(cookies)
        try:
            with self.opener.open(Request(url, headers=headers), timeout=10) as response:
                if kind == "headers":
                    return response.headers
                response_headers = response.headers if kind == "json_headers" else None
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            with exc:
                if kind == "redirect" and exc.code in (301, 302, 303, 307, 308):
                    return exc.headers.get("Location", "")
                raise Problem(f"http_{exc.code}", stop=(
                    exc.code == 429 or api_cookie and exc.code in (401, 403, 404, 410, 412))) from None
        except (URLError, OSError, ValueError, HTTPException) as exc:
            raise Problem(network_reason(exc)) from None
        if kind == "redirect":
            return ""
        if len(raw) > MAX_BYTES:
            raise Problem("response_too_large")
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            raise Problem("invalid_json") from None
        if not isinstance(value, dict):
            raise Problem("unexpected_json_shape")
        return (value, response_headers) if kind == "json_headers" else value

    def warm_up(self):
        try:
            headers = self.request(HOME_URL, kind="headers")
            for header in headers.get_all("Set-Cookie", []):
                cookie = SimpleCookie(header).get("buvid3")
                if cookie and COOKIE_VALUE.fullmatch(cookie.value):
                    self.buvid3 = cookie.value
                    break
        except Problem as exc:
            if exc.stop:
                raise
        except CookieError:
            pass

    def video(self, source):
        source = source.strip()
        if re.fullmatch(BV, source):
            return source, None
        if source.startswith(("b23.tv/", "bilibili.com/", "www.bilibili.com/", "m.bilibili.com/")):
            source = "https://" + source
        for _ in range(5):
            parts = https_parts(source, VIDEO_HOSTS | {SHORT_HOST})
            if parts.hostname != SHORT_HOST:
                match = re.fullmatch(fr"/video/({BV})/?", parts.path)
                if not match:
                    raise Problem("unsupported_input: use a BV ID or a Bilibili video URL")
                page = parse_qs(parts.query).get("p")
                if page and (not re.fullmatch(r"[1-9][0-9]{0,5}", page[0]) or len(page) != 1):
                    raise Problem("invalid_page_in_url")
                return match[1], int(page[0]) if page else None
            location = self.request(source, kind="redirect")
            if not location:
                break
            source = urljoin(source, location)
        raise Problem("unresolved_b23_link")

    def api(self, path, query):
        data = self.request(f"{API}{path}?{urlencode(query)}", api_cookie=True)
        code = data.get("code")
        if type(code) is not int or code != 0:
            detail = code if type(code) is int else "missing"
            raise Problem(f"api_{detail}", stop=code in (-101, -352, -412, -429, 352, 412, 429))
        if not isinstance(data.get("data"), dict):
            raise Problem("unexpected_api_data")
        return data["data"]

    def metadata(self, bvid):
        return self.api("/x/web-interface/view", {"bvid": bvid})

    def login_check(self):
        try:
            nav = self.api("/x/web-interface/nav", {})
        except Problem as exc:
            # Treat an explicit not-logged-in response as conclusive, not a
            # transport error, rate limit, or ambiguous HTTP denial.
            return "expired" if str(exc) == "api_-101" else "unverified"
        if nav.get("isLogin") is True:
            return "valid"
        if nav.get("isLogin") is False:
            return "expired"
        return "unverified"

    def player(self, bvid, cid):
        if self.last_player is not None:
            delay = self.last_player + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        try:
            return self.api(self.endpoint, {"bvid": bvid, "cid": cid})
        finally:
            self.last_player = time.monotonic()

    def subtitle(self, url):
        return self.request(url)


def qr_payload(response):
    code = response.get("code")
    if type(code) is not int or code != 0:
        raise Problem(f"qr_api_{code}" if type(code) is int else "qr_missing_api_code")
    data = response.get("data")
    if not isinstance(data, dict):
        raise Problem("qr_unexpected_api_data")
    return data


def qr_request(client, operation, *, key=None):
    url = f"{QR_API}/{operation}"
    if key is not None:
        url += f"?{urlencode({'qrcode_key': key})}"
    try:
        return client.request(url, kind="json_headers" if key is not None else "json")
    except Problem as exc:
        raise Problem(f"qr_{operation}_{exc}", stop=exc.stop) from None


def qr_session(response):
    data = qr_payload(response)
    url, key = data.get("url"), data.get("qrcode_key")
    if (not isinstance(url, str) or len(url) > 1500 or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)):
        raise Problem("qr_invalid_session")
    parts = https_parts(url, QR_SCAN_PATHS)
    if parts.path != QR_SCAN_PATHS[parts.hostname] or parse_qs(parts.query).get("qrcode_key") != [key]:
        raise Problem("qr_unexpected_login_link")
    return url, key


def qr_sessdata(headers):
    for header in headers.get_all("Set-Cookie", []):
        try:
            value = SimpleCookie(header).get("SESSDATA")
        except CookieError:
            continue
        if value and value.value:
            return checked_cookie(value.value)
    raise Problem("qr_confirmed_without_sessdata")


def write_qr_png(segno, url, path):
    image = segno.make_qr(url, error="m")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            image.save(file, kind="png", scale=8, border=4)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def qr_login(args):
    try:
        import segno
    except ImportError:
        raise Problem("qr_dependency_missing: install requirements-qr.txt") from None
    path = args.image.absolute()
    if path.exists():
        raise Problem("qr_image_already_exists: use a new path")
    cookie_path = args.sessdata_file or login_path()
    if path.resolve() == cookie_path.resolve():
        raise Problem("qr_image_and_sessdata_paths_must_differ")
    client = Client()
    deadline = time.monotonic() + QR_TTL
    url, key = qr_session(qr_request(client, "generate"))
    write_qr_png(segno, url, path)
    print(path, flush=True)
    print("用哔哩哔哩 App 扫码并在手机上确认。", file=sys.stderr, flush=True)

    previous = None
    while time.monotonic() < deadline:
        response, headers = qr_request(client, "poll", key=key)
        status = qr_payload(response).get("code")
        if type(status) is not int:
            raise Problem("qr_missing_poll_status")
        if status == 0:
            store_sessdata(cookie_path, qr_sessdata(headers))
            print("扫码登录成功。", file=sys.stderr, flush=True)
            return
        if status == 86038:
            break
        if status not in (86101, 86090):
            raise Problem(f"qr_poll_{status}")
        if status == 86090 and previous != status:
            print("已扫码，等待在手机上确认。", file=sys.stderr, flush=True)
        previous = status
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(QR_POLL_INTERVAL, remaining))
    raise Problem("qr_expired: generate a new QR code")


def number(value):
    # Convert only actual JSON numbers, not booleans or numeric strings.
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def positive_int(value):
    return type(value) is int and value > 0


def video_pages(metadata):
    rows = metadata.get("pages")
    if not rows:
        rows = [{"page": 1, "cid": metadata.get("cid"), "part": metadata.get("title"),
                 "duration": metadata.get("duration")}]
    if not isinstance(rows, list):
        raise Problem("unexpected_pages_shape")
    pages = {}
    for row in rows:
        if not isinstance(row, dict):
            raise Problem("unexpected_page_shape")
        page, cid = row.get("page"), row.get("cid")
        duration = number(row.get("duration"))
        if not positive_int(page) or not positive_int(cid) or not duration or duration <= 0 or page in pages:
            raise Problem("invalid_page_cid_or_duration")
        pages[page] = {"page": page, "cid": cid, "duration": duration, "part": label(row.get("part"))}
    return pages


def label(value):
    return value[:500] if isinstance(value, str) else ""


def parse_cues(body):
    if not isinstance(body, list) or not body:
        raise Problem("missing_or_empty_cues")
    cues = []
    for item in body:
        if not isinstance(item, dict):
            raise Problem("malformed_cue")
        start, end = number(item.get("from")), number(item.get("to"))
        content = item.get("content")
        if (start is None or end is None or not 0 <= start < end
                or end > 1_000_000_000 or not isinstance(content, str)):
            raise Problem("malformed_cue")
        if content.strip():
            cues.append({"from": start, "to": end, "content": content.strip()})
    if not cues:
        raise Problem("no_nonempty_cues")
    return sorted(cues, key=lambda cue: (cue["from"], cue["to"]))


def metrics(cues, duration):
    last_end = max(cue["to"] for cue in cues)
    coverage = last_end / duration
    return {"coverage": round(coverage, 6), "cue_count": len(cues),
            "first_start": cues[0]["from"], "last_end": last_end,
            "cue_per_minute": round(len(cues) / duration * 60, 2)}, (
            MIN_COVERAGE <= coverage <= MAX_COVERAGE)


def review_samples(cues, duration):
    middle = min(range(len(cues)), key=lambda i: abs(cues[i]["from"] - duration / 2))
    return {
        name: [{"at": cue["from"], "text": " ".join(cue["content"].split())[:180]} for cue in group]
        for name, group in (("head", cues[:3]), ("middle", cues[max(0, middle - 1):middle + 2]),
                            ("tail", cues[-3:]))
    }


def write_json(path, data):
    with path.open("w", encoding="utf-8", errors="backslashreplace") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")


def collect_page(client, bvid, page, out, *, rounds, lang, fallback):
    errors, tracks = [], {}
    empty_rounds = attempted = login_rounds = invalid_tracks = 0
    stopped = None
    for round_no in range(1, rounds + 1):
        attempted = round_no
        try:
            player = client.player(bvid, page["cid"])
            subtitle = player.get("subtitle")
            entries = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise Problem("unexpected_track_list")
            needs_login = player.get("need_login_subtitle") is True
            login_rounds += needs_login
        except Problem as exc:
            errors.append({"round": round_no, "reason": str(exc)})
            if exc.stop:
                stopped = str(exc)
                break
            continue
        if not entries:
            empty_rounds += 1
            if needs_login and not client.sessdata:
                break
            continue
        this_round = set()
        for entry in entries:
            if not isinstance(entry, dict):
                invalid_tracks += 1
                continue
            language = label(entry.get("lan"))
            try:
                url, track_id = subtitle_location(entry.get("subtitle_url"))
            except Problem:
                invalid_tracks += 1
                continue
            track = tracks.setdefault(track_id, {"id": track_id, "languages": [],
                                                  "labels": [], "first_round": round_no})
            if language and language not in track["languages"]:
                track["languages"].append(language)
            name = label(entry.get("lan_doc"))
            if name and name not in track["labels"]:
                track["labels"].append(name)
            if track_id in this_round or track.get("status") in {"candidate", "coverage_mismatch", "invalid_cues"}:
                continue
            if lang and not fallback and lang not in track["languages"]:
                track["status"] = "other_language"
                continue
            this_round.add(track_id)
            try:
                payload = client.subtitle(url)
            except Problem as exc:
                # A signed URL may expire; retry if the same path resurfaces in a later round.
                track["status"] = "download_error"
                track["download_error"] = str(exc)
                if exc.stop:
                    stopped = str(exc)
                    break
                continue
            track.pop("download_error", None)
            try:
                cues = parse_cues(payload.get("body"))
                measured, passes = metrics(cues, page["duration"])
            except Problem as exc:
                track.update(status="invalid_cues", reason=str(exc))
                continue
            track.update(measured)
            if not passes:
                track["status"] = "coverage_mismatch"
                continue
            path = f"candidates/P{page['page']}/{track_id}.json"
            candidate_path = out / path
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(candidate_path, {"bvid": bvid, **page, "track_id": track_id, "body": cues})
            track.update(status="candidate", file=path, samples=review_samples(cues, page["duration"]))
        if stopped:
            break

    def preference(track):
        languages = track["languages"]
        return (bool(lang and lang not in languages),
                any(item.startswith("ai-") for item in languages),
                not any(item.startswith(("zh", "ai-zh")) for item in languages),
                track["first_round"], track["id"])

    candidates = sorted((track for track in tracks.values() if track["status"] == "candidate"),
                        key=preference)
    if candidates:
        status = "needs_content_review"
    elif stopped in LOGIN_ERRORS or (login_rounds and empty_rounds == attempted):
        status = "login_required_or_expired"
    elif stopped:
        status = "request_stopped"
    elif tracks:
        status = "no_eligible_candidate"
    elif errors and empty_rounds == 0:
        status = "request_failed"
    elif invalid_tracks:
        status = "no_usable_track_url"
    else:
        status = "no_tracks_observed"
    return {**page, "status": status, "rounds_attempted": attempted, "empty_rounds": empty_rounds,
            "login_required_rounds": login_rounds, "invalid_track_entries": invalid_tracks,
            "stop_reason": stopped, "player_errors": errors,
            "candidate_order": [track["id"] for track in candidates], "tracks": list(tracks.values())}


def login_issue(check):
    return {"valid": "access_unavailable_with_valid_login",
            "unverified": "login_status_unverified"}.get(check, "login_required_or_expired")


def fetch(args):
    if not 1 <= args.rounds <= 12 or not 2 <= args.interval <= 60:
        raise Problem("use 1–12 rounds and an interval of 2–60 seconds")
    if args.out.exists():
        raise Problem("output_directory_already_exists: use a new path")
    sessdata, login_source = load_login(args.sessdata_file)
    client = Client(sessdata, args.interval, args.player)
    bvid, url_page = client.video(args.source)
    client.warm_up()
    try:
        metadata = client.metadata(bvid)
    except Problem as exc:
        if str(exc) not in LOGIN_ERRORS:
            raise
        check = client.login_check() if sessdata else None
        raise Problem(f"{login_issue(check)}: metadata {exc}, login_source={login_source}") from None
    pages = video_pages(metadata)
    requested = (sorted(pages) if args.all_pages else
                 list(dict.fromkeys(args.page or [url_page or 1])))
    if any(page not in pages for page in requested):
        raise Problem(f"page_not_found: available pages {', '.join(map(str, sorted(pages)))}")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"schema": 1, "bvid": bvid, "url": f"{HOME_URL}video/{bvid}/",
              "title": label(metadata.get("title")), "description": label(metadata.get("desc")),
              "uploader": label((metadata.get("owner") or {}).get("name"))
              if isinstance(metadata.get("owner"), dict) else "",
              "available_pages": sorted(pages), "requested_pages": requested,
              "requested_language": args.lang, "allow_language_fallback": args.fallback_language,
              "player_endpoint": client.endpoint, "login_source": login_source, "pages": []}
    manifest = args.out / "report.json"
    stopped = None
    for index in requested:
        if stopped:
            page = {**pages[index], "status": "not_attempted", "stop_reason": stopped}
        else:
            page = collect_page(client, bvid, pages[index], args.out, rounds=args.rounds,
                                lang=args.lang, fallback=args.fallback_language)
            stopped = page["stop_reason"]
        report["pages"].append(page)
        write_json(manifest, report)
    gated = [page for page in report["pages"] if page["status"] == "login_required_or_expired"]
    if sessdata and gated:
        # Probe only after a login-specific signal, and never during a rate limit.
        check = "unverified" if stopped and stopped not in LOGIN_ERRORS else client.login_check()
        report["login_check"] = check
        for page in gated:
            page["status"] = login_issue(check)
        write_json(manifest, report)
    print(manifest)
    return 0 if all(p["status"] == "needs_content_review" for p in report["pages"]) else 2


def timestamp(seconds, *, srt=False):
    millis = round(seconds * 1000) if srt else int(seconds) * 1000
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, rem = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}" + (f",{rem:03}" if srt else "")


def render(args):
    try:
        data = json.loads(args.candidate.read_text(encoding="utf-8"))
        cues = parse_cues(data.get("body") if isinstance(data, dict) else None)
    except (OSError, ValueError, RecursionError):
        raise Problem("cannot_read_candidate") from None
    for i, cue in enumerate(cues, 1):
        content = "\n".join(line for line in cue["content"].splitlines() if line.strip())
        if args.format == "srt":
            start = timestamp(cue["from"], srt=True)
            end = timestamp(max(cue["to"], (round(cue["from"] * 1000) + 1) / 1000), srt=True)
            print(f"{i}\n{start} --> {end}\n{content}\n")
        elif args.format == "timed":
            print(f"[{timestamp(cue['from'])}] {' '.join(content.split())}")
        else:
            print(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    grab = sub.add_parser("fetch", help="collect duration-qualified candidates and diagnostics")
    grab.add_argument("source", help="BV ID, video URL, or b23.tv link")
    grab.add_argument("--out", type=Path, required=True, help="new directory for report and candidates")
    page_group = grab.add_mutually_exclusive_group()
    page_group.add_argument("--page", type=int, action="append", help="repeat to select multiple pages")
    page_group.add_argument("--all-pages", action="store_true")
    grab.add_argument("--lang", help="exact Bilibili language code, e.g. ai-zh")
    grab.add_argument("--fallback-language", action="store_true", help="also collect other languages")
    grab.add_argument("--rounds", type=int, default=12, help="player requests per page (default: 12)")
    grab.add_argument("--interval", type=float, default=2, help="minimum seconds between player calls")
    grab.add_argument("--player", choices=("legacy", "wbi"), default="legacy",
                      help="use legacy v2 or the compatibility endpoint wbi/v2 (unsigned)")
    grab.add_argument("--sessdata-file", type=Path, help="private file containing only the cookie value")
    login = sub.add_parser("login", help="save SESSDATA locally via a hidden interactive prompt")
    login.add_argument("--sessdata-file", type=Path)
    qr = sub.add_parser("qr-login", help="write a Bilibili login QR PNG and wait for confirmation")
    qr.add_argument("--image", type=Path, required=True, help="new PNG path for the QR code")
    qr.add_argument("--sessdata-file", type=Path, help="override the persistent local login file")
    export = sub.add_parser("render", help="format the chosen candidate without contacting Bilibili")
    export.add_argument("candidate", type=Path)
    export.add_argument("--format", choices=("timed", "text", "srt"), default="timed")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return fetch(args)
        if args.command == "login":
            save_login(args.sessdata_file or login_path())
        elif args.command == "qr-login":
            qr_login(args)
        else:
            render(args)
        return 0
    except Problem as exc:
        print(f"bilibili-subtitle-summary: {exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("bilibili-subtitle-summary: local_io_error", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("bilibili-subtitle-summary: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
