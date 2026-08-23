import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

import collector


NOW = datetime(2026, 8, 23, 10, tzinfo=UTC)


def row(contest_id, resource_id, start, end, **overrides):
    value = {
        "id": contest_id,
        "resource_id": resource_id,
        "event": f"Contest {contest_id}",
        "href": f"https://example.com/{contest_id}",
        "start": start,
        "end": end,
    }
    value.update(overrides)
    return value


class FakeResponse:
    def __init__(self, value, *, encoded=False):
        self.payload = value if encoded else json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)

    def build_url(self, endpoint, params=None):
        return endpoint if not params else f"{endpoint}?{len(params)}"

    def get_json(self, _url):
        value = next(self.pages)
        if isinstance(value, Exception):
            raise value
        return value


class CollectorTests(unittest.TestCase):
    def test_pagination_and_loop_detection(self):
        pages = [
            {"objects": [{"id": 1}], "meta": {"next": "second"}},
            {"objects": [{"id": 2}], "meta": {"next": None}},
        ]
        self.assertEqual([1, 2], [item["id"] for item in collector.paginate(FakeClient(pages), "first", {})])

        loop = FakeClient([{"objects": [{"id": 1}], "meta": {"next": "first"}}])
        with self.assertRaisesRegex(collector.CollectorError, "loop"):
            list(collector.paginate(loop, "first", {}))

        invalid_pages = [
            {"objects": [], "meta": {"next": "second"}},
            {"objects": [], "meta": "invalid"},
            {"objects": ["invalid"], "meta": {"next": None}},
        ]
        for page in invalid_pages:
            with self.subTest(page=page), self.assertRaises(collector.CollectorError):
                list(collector.paginate(FakeClient([page]), "first", {}))

    def test_resource_hosts_are_resolved_exactly(self):
        objects = [
            {"id": number, "name": host}
            for number, host in enumerate(collector.PLATFORMS, start=1)
        ]
        resolved = collector.resolve_resources(
            FakeClient([{"objects": objects, "meta": {"next": None}}])
        )
        self.assertEqual(set(collector.PLATFORMS.values()), set(resolved.values()))

        with self.assertRaisesRegex(collector.CollectorError, "could not be resolved"):
            collector.resolve_resources(
                FakeClient([{"objects": objects[:-1], "meta": {"next": None}}])
            )

    def test_normalization_deduplicates_sorts_and_validates(self):
        resources = {1: "codeforces", 2: "atcoder"}
        rows = [
            row(2, 1, "2026-08-25T12:00:00+02:00", "2026-08-25T13:00:00+02:00"),
            row(1, 2, "2026-08-24T10:00:00", "2026-08-24T11:00:00"),
            row(1, 2, "2026-08-24T10:00:00", "2026-08-24T11:00:00"),
        ]
        contests = collector.normalize_contests(rows, resources)
        self.assertEqual(["clist:1", "clist:2"], [contest["id"] for contest in contests])
        self.assertEqual("2026-08-25T10:00:00Z", contests[1]["startsAt"])

        bad_rows = [
            row(None, 1, "2026-08-24T10:00:00", "2026-08-24T11:00:00"),
            row(1, 9, "2026-08-24T10:00:00", "2026-08-24T11:00:00"),
            row(1, 1, "bad", "2026-08-24T11:00:00"),
            row(1, 1, "2026-08-24T12:00:00", "2026-08-24T11:00:00"),
            row(1, 1, "2026-08-24T10:00:00", "2026-08-24T11:00:00", href="javascript:x"),
        ]
        for bad in bad_rows:
            with self.subTest(bad=bad), self.assertRaises(collector.CollectorError):
                collector.normalize_contests([bad], resources)

    def test_rolling_archive_digest_and_stable_no_change(self):
        resources = {1: "codeforces"}
        contests = collector.normalize_contests(
            [
                row(1, 1, "2026-08-24T10:00:00Z", "2026-08-24T12:00:00Z"),
                row(2, 1, "2026-08-01T10:00:00Z", "2026-08-01T12:00:00Z"),
                row(3, 1, "2026-01-01T10:00:00Z", "2026-01-01T12:00:00Z"),
            ],
            resources,
        )
        outputs = collector.build_outputs(
            contests, {}, {}, [], now=NOW, since=NOW - timedelta(days=183), incremental=False
        )
        main = json.loads(outputs["data/contests.json"])
        index = json.loads(outputs["data/archive/index.json"])
        self.assertEqual(["clist:1"], [item["id"] for item in main["contests"]])
        self.assertEqual(1, len(index["archives"]))
        entry = index["archives"][0]
        self.assertEqual(hashlib.sha256(outputs[entry["path"]]).hexdigest(), entry["sha256"])

        second = collector.build_outputs(
            contests,
            main,
            index,
            json.loads(outputs[entry["path"]])["contests"],
            now=NOW + timedelta(hours=3),
            since=NOW - timedelta(days=7),
            incremental=True,
        )
        self.assertEqual(main["updatedAt"], json.loads(second["data/contests.json"])["updatedAt"])
        self.assertEqual(index["updatedAt"], json.loads(second["data/archive/index.json"])["updatedAt"])

    def test_incremental_preserves_older_archive_and_replaces_recent(self):
        resources = {1: "codeforces"}
        old = collector.normalize_contests(
            [row(10, 1, "2026-07-01T10:00:00Z", "2026-07-01T12:00:00Z")], resources
        )[0]
        fetched = collector.normalize_contests(
            [
                row(11, 1, "2026-08-20T10:00:00Z", "2026-08-20T12:00:00Z"),
                row(12, 1, "2026-08-25T10:00:00Z", "2026-08-25T12:00:00Z"),
            ],
            resources,
        )
        outputs = collector.build_outputs(
            fetched, {}, {}, [old], now=NOW, since=NOW - timedelta(days=7), incremental=True
        )
        index = json.loads(outputs["data/archive/index.json"])
        ids = {
            contest["id"]
            for entry in index["archives"]
            for contest in json.loads(outputs[entry["path"]])["contests"]
        }
        self.assertEqual({"clist:10", "clist:11"}, ids)

    def test_retry_retry_after_auth_and_network_failure(self):
        calls = []
        responses = [
            HTTPError("https://clist.by", 429, "busy", {"Retry-After": "3"}, io.BytesIO()),
            FakeResponse({"objects": [], "meta": {"next": None}}),
        ]

        def opener(*_args, **_kwargs):
            value = responses.pop(0)
            calls.append(value)
            if isinstance(value, Exception):
                raise value
            return value

        sleeps = []
        client = collector.ApiClient("user", "key", opener=opener, sleep=sleeps.append, min_interval=0)
        self.assertIn("meta", client.get_json("contest/"))
        self.assertEqual([3.0], sleeps)
        self.assertEqual(2, len(calls))

        auth = collector.ApiClient(
            "user",
            "key",
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                HTTPError("https://clist.by", 401, "no", {}, None)
            ),
            min_interval=0,
        )
        with self.assertRaisesRegex(collector.CollectorError, "credentials"):
            auth.get_json("contest/")

        network = collector.ApiClient(
            "user",
            "key",
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
            sleep=lambda _seconds: None,
            min_interval=0,
        )
        with self.assertRaisesRegex(collector.CollectorError, "four attempts"):
            network.get_json("contest/")

        malformed = collector.ApiClient(
            "user",
            "key",
            opener=lambda *_args, **_kwargs: FakeResponse(b"not-json", encoded=True),
            min_interval=0,
        )
        with self.assertRaisesRegex(collector.CollectorError, "malformed JSON"):
            malformed.get_json("contest/")

        server_responses = [
            HTTPError("https://clist.by", 503, "busy", {}, None),
            FakeResponse({"ok": True}),
        ]

        def server_opener(*_args, **_kwargs):
            value = server_responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        server = collector.ApiClient(
            "user", "key", opener=server_opener, sleep=lambda _seconds: None, min_interval=0
        )
        self.assertTrue(server.get_json("contest/")["ok"])

    def test_invalid_existing_data_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data" / "contests.json"
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            before = path.read_bytes()
            client = FakeClient([])
            with self.assertRaises(collector.CollectorError):
                collector.collect("reconcile", root, client, NOW)
            self.assertEqual(before, path.read_bytes())

    def test_write_outputs_is_atomic_and_noop_when_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = {
                "data/contests.json": b"{}\n",
                "data/archive/index.json": b"{}\n",
            }
            self.assertEqual(2, len(collector.write_outputs(root, outputs)))
            self.assertEqual([], collector.write_outputs(root, outputs))


if __name__ == "__main__":
    unittest.main()
