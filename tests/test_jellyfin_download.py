import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "jellyfin-download.py"
spec = importlib.util.spec_from_file_location("jellyfin_download", SCRIPT)
jd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jd)


def episodes(layout):
    """layout: {season: episode_count}"""
    return [{"Id": f"{s}-{e}", "ParentIndexNumber": s, "IndexNumber": e}
            for s, n in layout.items() for e in range(1, n + 1)]


def codes(eps):
    return [jd.ep_code(ep) for ep in eps]


class RangeTests(unittest.TestCase):
    eps = episodes({0: 1, 1: 7, 2: 10, 3: 4})

    def select(self, spec):
        return codes(jd.filter_episodes(self.eps, spec))

    def test_no_range_selects_everything(self):
        self.assertEqual(len(self.select(None)), 22)

    def test_episode_to_episode_across_seasons(self):
        got = self.select("s1e1-s2e8")
        self.assertEqual((len(got), got[0], got[-1]), (15, "S01E01", "S02E08"))

    def test_season_to_season(self):
        got = self.select("s1-s2")
        self.assertEqual((len(got), got[0], got[-1]), (17, "S01E01", "S02E10"))

    def test_single_season(self):
        self.assertEqual(self.select("s3"), ["S03E01", "S03E02", "S03E03", "S03E04"])

    def test_single_episode(self):
        self.assertEqual(self.select("s2e5"), ["S02E05"])

    def test_episode_range_within_season(self):
        self.assertEqual(self.select("s1e3-e7"), ["S01E03", "S01E04", "S01E05", "S01E06", "S01E07"])

    def test_open_ended(self):
        self.assertEqual(len(self.select("s2e9-")), 6)

    def test_specials(self):
        self.assertEqual(self.select("s0"), ["S00E01"])

    def test_combined_and_case_insensitive(self):
        self.assertEqual(self.select("S1E6-E7, s3e4"), ["S01E06", "S01E07", "S03E04"])

    def test_invalid_ranges(self):
        for spec in ("x1", "s2-s1", "e5", "s1e", ","):
            with self.subTest(spec=spec), self.assertRaises(jd.JellyfinError):
                jd.filter_episodes(self.eps, spec)

    def test_no_matches(self):
        with self.assertRaises(jd.JellyfinError):
            jd.filter_episodes(self.eps, "s3e9")


class HelperTests(unittest.TestCase):
    def test_content_disposition(self):
        cd = jd.content_disposition_filename
        self.assertEqual(cd("attachment; filename*=UTF-8''Caf%C3%A9%20S01E01.mkv"), "Café S01E01.mkv")
        self.assertEqual(cd('attachment; filename="Show S01E01.mkv"'), "Show S01E01.mkv")
        self.assertEqual(cd("attachment; filename=plain.mkv"), "plain.mkv")
        self.assertIsNone(cd(None))

    def test_sanitize(self):
        self.assertEqual(jd.sanitize(' A/B: "C"? '), "A_B_ _C__")

    def test_with_year_does_not_duplicate(self):
        self.assertEqual(jd.with_year({"Name": "Dune", "ProductionYear": 2021}), "Dune (2021)")
        self.assertEqual(jd.with_year({"Name": "Young Sherlock (2026)", "ProductionYear": 2026}),
                         "Young Sherlock (2026)")

    def test_double_episode_code(self):
        ep = {"ParentIndexNumber": 1, "IndexNumber": 1, "IndexNumberEnd": 2}
        self.assertEqual(jd.ep_code(ep), "S01E01-E02")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        for key in [k for k in os.environ if k.startswith("JELLYFIN_")]:
            del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write(text)
        try:
            jd.load_config(Path(f.name))
        finally:
            os.unlink(f.name)

    def test_parses_quoting_and_export(self):
        self.load('# comment\nexport JELLYFIN_URL="http://a.example/"\nJELLYFIN_API_KEY=\'abc def\'\n')
        self.assertEqual(os.environ["JELLYFIN_URL"], "http://a.example/")
        self.assertEqual(os.environ["JELLYFIN_API_KEY"], "abc def")

    def test_ignores_other_variables_and_does_not_execute(self):
        path = os.environ["PATH"]
        self.load("PATH=/evil\nJELLYFIN_OP_ITEM=$(touch /tmp/pwned)\n")
        self.assertEqual(os.environ["PATH"], path)
        self.assertEqual(os.environ["JELLYFIN_OP_ITEM"], "$(touch /tmp/pwned)")

    def test_environment_wins(self):
        os.environ["JELLYFIN_URL"] = "http://env.example"
        self.load("JELLYFIN_URL=http://file.example\n")
        self.assertEqual(os.environ["JELLYFIN_URL"], "http://env.example")


class FakeResponse:
    """Minimal urlopen() response. `fail` makes read() raise once the data
    runs out, like a connection reset; otherwise the stream just ends."""

    def __init__(self, body, start, total, fail=False, status=None):
        self.body, self.fail = io.BytesIO(body), fail
        self.status = status or (206 if start else 200)
        self.headers = {"Content-Range": f"bytes {start}-{total - 1}/{total}",
                        "Content-Disposition": 'attachment; filename="t.bin"'}

    def read(self, n):
        data = self.body.read(n)
        if not data and self.fail:
            raise ConnectionResetError("reset by peer")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FlakyAPI:
    """Serves `data`; `script` lists one behaviour per full-file request:
    "503" (transient error), "cut" (stream ends early), "reset" (read raises),
    or "ok". The 1-byte probe request is always served."""

    def __init__(self, data, script, cut_at=1000):
        self.data, self.script, self.cut_at = data, list(script), cut_at
        self.requests = 0

    def request(self, path, params=None, headers=None):
        rng = (headers or {}).get("Range", "")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        if rng == "bytes=0-0":
            return FakeResponse(self.data[:1], 0, len(self.data), status=206)
        self.requests += 1
        action = self.script.pop(0) if self.script else "ok"
        if action == "503":
            raise jd.TransientError("503 Service Unavailable")
        body = self.data[start:]
        if action in ("cut", "reset"):
            return FakeResponse(body[:self.cut_at], start, len(self.data), fail=action == "reset")
        return FakeResponse(body, start, len(self.data))


class RetryTests(unittest.TestCase):
    data = bytes(range(256)) * 20

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patch = mock.patch.object(jd.time, "sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        for stream in ("sys.stdout", "sys.stderr"):
            patch = mock.patch(stream, io.StringIO())
            patch.start()
            self.addCleanup(patch.stop)

    def download(self, script, **kw):
        api = FlakyAPI(self.data, script, **kw)
        jd.download_file(api, "x", "fallback.bin", self.dir, "[1/1]")
        return api

    def test_clean_download(self):
        api = self.download([])
        self.assertEqual((self.dir / "t.bin").read_bytes(), self.data)
        self.assertEqual(api.requests, 1)

    def test_resumes_after_truncated_stream(self):
        api = self.download(["cut", "cut"])
        self.assertEqual((self.dir / "t.bin").read_bytes(), self.data)
        self.assertEqual(api.requests, 3)

    def test_resumes_after_connection_reset(self):
        self.download(["reset"])
        self.assertEqual((self.dir / "t.bin").read_bytes(), self.data)

    def test_retries_transient_server_error(self):
        self.download(["503", "ok"])
        self.assertEqual((self.dir / "t.bin").read_bytes(), self.data)
        self.sleep.assert_called_once_with(1)

    def test_gives_up_without_progress(self):
        with self.assertRaises(jd.JellyfinError) as ctx:
            self.download(["503"] * (jd.MAX_RETRIES + 1))
        self.assertIn("giving up", str(ctx.exception))

    def test_failure_counter_resets_on_progress(self):
        # More cuts than MAX_RETRIES, but each one makes progress.
        self.download(["cut"] * (jd.MAX_RETRIES + 5), cut_at=100)
        self.assertEqual((self.dir / "t.bin").read_bytes(), self.data)

    def test_backoff_is_capped(self):
        self.download(["503"] * jd.MAX_RETRIES)
        delays = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(delays, [min(2 ** i, jd.MAX_BACKOFF) for i in range(jd.MAX_RETRIES)])


if __name__ == "__main__":
    unittest.main()
