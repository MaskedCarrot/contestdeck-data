#!/usr/bin/env python3
"""Build ContestDeck's public contest snapshot from the CList API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


API_BASE = "https://clist.by/api/v2/"
PAGE_LIMIT = 1000
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_INTERVAL_SECONDS = 6.1
MAX_ATTEMPTS = 4
RETENTION_DAYS = 183
INCREMENTAL_LOOKBACK_DAYS = 7
SCHEMA_VERSION = 1
SOURCE = {"name": "CLIST", "url": "https://clist.by"}

PLATFORMS = {
    "codeforces.com": "codeforces",
    "codechef.com": "codechef",
    "atcoder.jp": "atcoder",
    "leetcode.com": "leetcode",
    "hackerrank.com": "hackerrank",
    "hackerearth.com": "hackerearth",
    "topcoder.com": "topcoder",
    "facebook.com/hackercup": "meta-hacker-cup",
    "icpc.global": "icpc",
    "ac.nowcoder.com": "nowcoder",
    "luogu.com.cn": "luogu",
    "cphof.org": "cphof",
}

CONTEST_KEYS = {"id", "platform", "name", "url", "startsAt", "endsAt"}


class CollectorError(RuntimeError):
    """A safe, user-facing collection failure."""


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CollectorError("contest timestamp is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CollectorError(f"invalid contest timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"cannot read existing data file {path}") from exc


class ApiClient:
    def __init__(
        self,
        username: str,
        api_key: str,
        *,
        base_url: str = API_BASE,
        opener: Callable[..., Any] = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if not username or not api_key:
            raise CollectorError("CLIST_USERNAME and CLIST_API_KEY are required")
        self.username = username
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") + "/"
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.min_interval = min_interval
        self.last_request: float | None = None
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CollectorError("invalid CList API base URL")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

    def build_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        url = urljoin(self.base_url, endpoint)
        parsed = urlparse(url)
        base = urlparse(self.base_url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise CollectorError("CList pagination attempted to leave the API host")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params:
            query.update({key: str(value) for key, value in params.items()})
        query.setdefault("username", self.username)
        query.setdefault("api_key", self.api_key)
        query.setdefault("format", "json")
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _wait_for_slot(self) -> None:
        if self.last_request is None:
            return
        wait = self.min_interval - (self.clock() - self.last_request)
        if wait > 0:
            self.sleep(wait)

    def _retry_delay(self, attempt: int, headers: Any = None) -> float:
        retry_after = headers.get("Retry-After") if headers else None
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    target = parsedate_to_datetime(retry_after)
                    return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(2**attempt)

    def get_json(self, url: str) -> dict[str, Any]:
        authenticated_url = self.build_url(url)
        request = Request(
            authenticated_url,
            headers={"Accept": "application/json", "User-Agent": "ContestDeck-Data/1"},
        )
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_slot()
            self.last_request = self.clock()
            try:
                with self.opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    payload = response.read()
                try:
                    decoded = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CollectorError("CList returned malformed JSON") from exc
                if not isinstance(decoded, dict):
                    raise CollectorError("CList returned an unexpected JSON document")
                return decoded
            except HTTPError as exc:
                code = exc.code
                headers = exc.headers
                exc.close()
                if code in {401, 403}:
                    raise CollectorError("CList rejected the configured credentials") from exc
                retryable = code == 429 or 500 <= code < 600
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise CollectorError(f"CList request failed with HTTP {code}") from exc
                self.sleep(self._retry_delay(attempt, headers))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise CollectorError("CList could not be reached after four attempts") from exc
                self.sleep(self._retry_delay(attempt))
        raise AssertionError("unreachable")


def paginate(client: ApiClient, endpoint: str, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    url = client.build_url(endpoint, params)
    seen: set[str] = set()
    for _ in range(1000):
        if url in seen:
            raise CollectorError("CList pagination loop detected")
        seen.add(url)
        document = client.get_json(url)
        objects = document.get("objects")
        meta = document.get("meta")
        if not isinstance(objects, list) or not isinstance(meta, dict):
            raise CollectorError("CList response is missing objects or pagination metadata")
        if not objects and meta.get("next"):
            raise CollectorError("CList returned an empty intermediate page")
        for item in objects:
            if not isinstance(item, dict):
                raise CollectorError("CList returned a non-object record")
            yield item
        next_page = meta.get("next")
        if next_page in {None, ""}:
            return
        if not isinstance(next_page, str):
            raise CollectorError("CList returned invalid pagination metadata")
        url = client.build_url(urljoin(url, next_page))
    raise CollectorError("CList pagination exceeded the safety limit")


def resolve_resources(client: ApiClient) -> dict[int, str]:
    resources = list(paginate(client, "resource/", {"limit": PAGE_LIMIT, "order_by": "id"}))
    by_host: dict[str, int] = {}
    for resource in resources:
        host = resource.get("name")
        resource_id = resource.get("id")
        if isinstance(host, str) and isinstance(resource_id, int):
            if host in by_host and by_host[host] != resource_id:
                raise CollectorError(f"CList returned duplicate resource host {host}")
            by_host[host] = resource_id
    missing = sorted(set(PLATFORMS) - set(by_host))
    if missing:
        raise CollectorError(f"CList resources could not be resolved: {', '.join(missing)}")
    return {by_host[host]: slug for host, slug in PLATFORMS.items()}


def fetch_contests(client: ApiClient, resources: dict[int, str], since: datetime) -> list[dict[str, Any]]:
    rows = list(
        paginate(
            client,
            "contest/",
            {
                "limit": PAGE_LIMIT,
                "order_by": "start",
                "end__gte": format_time(since),
                "resource_id__in": ",".join(str(value) for value in sorted(resources)),
                "filtered": "false",
            },
        )
    )
    if not rows:
        raise CollectorError("CList returned no contests")
    return rows


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise CollectorError(f"contest {label} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"contest {label} is invalid") from exc
    if parsed <= 0:
        raise CollectorError(f"contest {label} is invalid")
    return parsed


def normalize_contests(rows: Iterable[dict[str, Any]], resources: dict[int, str]) -> list[dict[str, str]]:
    contests: dict[str, dict[str, str]] = {}
    for row in rows:
        contest_id = positive_int(row.get("id"), "id")
        resource_id = positive_int(row.get("resource_id"), "resource id")
        if resource_id not in resources:
            raise CollectorError(f"contest {contest_id} references an unexpected resource")
        name = row.get("event")
        url = row.get("href")
        if not isinstance(name, str) or not name.strip():
            raise CollectorError(f"contest {contest_id} has no name")
        if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
            raise CollectorError(f"contest {contest_id} has an invalid URL")
        starts_at = parse_time(row.get("start"))
        ends_at = parse_time(row.get("end"))
        if ends_at < starts_at:
            raise CollectorError(f"contest {contest_id} ends before it starts")
        normalized = {
            "id": f"clist:{contest_id}",
            "platform": resources[resource_id],
            "name": name.strip(),
            "url": url,
            "startsAt": format_time(starts_at),
            "endsAt": format_time(ends_at),
        }
        key = normalized["id"]
        if key in contests and contests[key] != normalized:
            raise CollectorError(f"CList returned conflicting records for {key}")
        contests[key] = normalized
    return sorted(contests.values(), key=lambda item: (item["startsAt"], item["platform"], item["id"]))


def validate_public_contest(contest: Any) -> None:
    if not isinstance(contest, dict) or set(contest) != CONTEST_KEYS:
        raise CollectorError("generated contest does not match schema version 1")
    if not isinstance(contest["id"], str) or not contest["id"].startswith("clist:"):
        raise CollectorError("generated contest has an invalid id")
    if contest["platform"] not in PLATFORMS.values():
        raise CollectorError("generated contest has an invalid platform")
    if not isinstance(contest["name"], str) or not contest["name"]:
        raise CollectorError("generated contest has an invalid name")
    if urlparse(contest["url"]).scheme not in {"http", "https"}:
        raise CollectorError("generated contest has an invalid URL")
    if parse_time(contest["endsAt"]) < parse_time(contest["startsAt"]):
        raise CollectorError("generated contest ends before it starts")


def load_existing(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    main = read_json(data_dir / "contests.json", {})
    index = read_json(data_dir / "archive" / "index.json", {})
    archived: list[dict[str, str]] = []
    archive_dir = data_dir / "archive"
    if archive_dir.exists():
        for path in sorted(archive_dir.glob("????-??.json")):
            document = read_json(path, {})
            if document.get("schemaVersion") != SCHEMA_VERSION or document.get("month") != path.stem:
                raise CollectorError(f"existing archive {path} has an invalid envelope")
            contests = document.get("contests")
            if not isinstance(contests, list):
                raise CollectorError(f"existing archive {path} has no contest list")
            for contest in contests:
                validate_public_contest(contest)
                archived.append(contest)
    return main, index, archived


def stable_updated_at(previous: dict[str, Any], field: str, value: Any, now: datetime) -> str:
    previous_value = previous.get("updatedAt")
    if previous.get(field) == value and isinstance(previous_value, str):
        parse_time(previous_value)
        return previous_value
    return format_time(now)


def build_outputs(
    contests: list[dict[str, str]],
    existing_main: dict[str, Any],
    existing_index: dict[str, Any],
    existing_archive: list[dict[str, str]],
    *,
    now: datetime,
    since: datetime,
    incremental: bool,
) -> dict[str, bytes]:
    now = now.astimezone(UTC).replace(microsecond=0)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    current = [contest for contest in contests if parse_time(contest["endsAt"]) > now]
    if not current:
        raise CollectorError("CList returned no running or future contests; refusing to erase the snapshot")

    archived_by_id: dict[str, dict[str, str]] = {}
    if incremental:
        for contest in existing_archive:
            end = parse_time(contest["endsAt"])
            if cutoff <= end < since:
                archived_by_id[contest["id"]] = contest
    for contest in contests:
        end = parse_time(contest["endsAt"])
        archived_by_id.pop(contest["id"], None)
        if cutoff <= end <= now:
            archived_by_id[contest["id"]] = contest

    archive = sorted(
        archived_by_id.values(), key=lambda item: (item["endsAt"], item["platform"], item["id"])
    )
    groups: dict[str, list[dict[str, str]]] = {}
    for contest in archive:
        groups.setdefault(contest["endsAt"][:7], []).append(contest)

    main_content = {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": stable_updated_at(existing_main, "contests", current, now),
        "source": SOURCE,
        "retentionDays": RETENTION_DAYS,
        "contests": current,
    }
    outputs = {"data/contests.json": json_bytes(main_content)}
    archives: list[dict[str, Any]] = []
    for month, month_contests in sorted(groups.items()):
        path = f"data/archive/{month}.json"
        payload = json_bytes(
            {"schemaVersion": SCHEMA_VERSION, "month": month, "contests": month_contests}
        )
        outputs[path] = payload
        archives.append(
            {
                "month": month,
                "path": path,
                "count": len(month_contests),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    index_content = {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": stable_updated_at(existing_index, "archives", archives, now),
        "archives": archives,
    }
    outputs["data/archive/index.json"] = json_bytes(index_content)
    validate_outputs(outputs, now)
    return outputs


def validate_outputs(outputs: dict[str, bytes], now: datetime) -> None:
    try:
        main = json.loads(outputs["data/contests.json"])
        index = json.loads(outputs["data/archive/index.json"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise CollectorError("generated output is not valid JSON") from exc
    if (
        main.get("schemaVersion") != SCHEMA_VERSION
        or main.get("source") != SOURCE
        or main.get("retentionDays") != RETENTION_DAYS
        or not isinstance(main.get("contests"), list)
    ):
        raise CollectorError("generated snapshot has an invalid envelope")
    parse_time(main.get("updatedAt"))
    current_ids: set[str] = set()
    for contest in main["contests"]:
        validate_public_contest(contest)
        if parse_time(contest["endsAt"]) <= now:
            raise CollectorError("generated snapshot contains an ended contest")
        if contest["id"] in current_ids:
            raise CollectorError("generated snapshot contains duplicate ids")
        current_ids.add(contest["id"])
    if main["contests"] != sorted(
        main["contests"], key=lambda item: (item["startsAt"], item["platform"], item["id"])
    ):
        raise CollectorError("generated snapshot is not sorted")

    if index.get("schemaVersion") != SCHEMA_VERSION or not isinstance(index.get("archives"), list):
        raise CollectorError("generated archive index has an invalid envelope")
    parse_time(index.get("updatedAt"))
    cutoff = now - timedelta(days=RETENTION_DAYS)
    archived_ids: set[str] = set()
    for entry in index["archives"]:
        if set(entry) != {"month", "path", "count", "sha256"}:
            raise CollectorError("generated archive index entry is invalid")
        payload = outputs.get(entry["path"])
        if payload is None or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise CollectorError("generated archive digest does not match")
        document = json.loads(payload)
        if document.get("schemaVersion") != SCHEMA_VERSION or document.get("month") != entry["month"]:
            raise CollectorError("generated archive has an invalid envelope")
        month_contests = document.get("contests")
        if not isinstance(month_contests, list) or len(month_contests) != entry["count"]:
            raise CollectorError("generated archive count does not match")
        for contest in month_contests:
            validate_public_contest(contest)
            end = parse_time(contest["endsAt"])
            if not cutoff <= end <= now or contest["endsAt"][:7] != entry["month"]:
                raise CollectorError("generated archive contest is outside its retention window")
            if contest["id"] in archived_ids or contest["id"] in current_ids:
                raise CollectorError("generated data contains duplicate contest ids")
            archived_ids.add(contest["id"])


def changed_paths(root: Path, outputs: dict[str, bytes]) -> tuple[list[str], list[Path]]:
    changed = []
    for relative, payload in outputs.items():
        path = root / relative
        if not path.exists() or path.read_bytes() != payload:
            changed.append(relative)
    archive_dir = root / "data" / "archive"
    wanted = {root / relative for relative in outputs if relative.startswith("data/archive/")}
    stale = [path for path in archive_dir.glob("????-??.json") if path not in wanted] if archive_dir.exists() else []
    return sorted(changed), sorted(stale)


def write_outputs(root: Path, outputs: dict[str, bytes]) -> list[str]:
    changed, stale = changed_paths(root, outputs)
    if not changed and not stale:
        return []
    with tempfile.TemporaryDirectory(prefix="contestdeck-", dir=root) as temporary:
        temporary_root = Path(temporary)
        for relative in changed:
            staged = temporary_root / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(outputs[relative])
        for relative in changed:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_root / relative, target)
    for path in stale:
        path.unlink()
    return changed + [str(path.relative_to(root)) for path in stale]


def collect(mode: str, root: Path, client: ApiClient, now: datetime) -> tuple[dict[str, bytes], bool]:
    data_dir = root / "data"
    existing_main, existing_index, existing_archive = load_existing(data_dir)
    reconcile = mode in {"reconcile", "dry-run"} or not existing_archive
    since = now - timedelta(days=RETENTION_DAYS if reconcile else INCREMENTAL_LOOKBACK_DAYS)
    resources = resolve_resources(client)
    rows = fetch_contests(client, resources, since)
    contests = normalize_contests(rows, resources)
    outputs = build_outputs(
        contests,
        existing_main,
        existing_index,
        existing_archive,
        now=now,
        since=since,
        incremental=not reconcile,
    )
    return outputs, reconcile


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("incremental", "reconcile", "dry-run"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        client = ApiClient(
            os.environ.get("CLIST_USERNAME", ""),
            os.environ.get("CLIST_API_KEY", ""),
            base_url=os.environ.get("CLIST_API_BASE", API_BASE),
        )
        now = datetime.now(UTC).replace(microsecond=0)
        outputs, reconciled = collect(args.mode, args.root.resolve(), client, now)
        changed, stale = changed_paths(args.root.resolve(), outputs)
        if args.mode == "dry-run":
            paths = changed + [str(path.relative_to(args.root.resolve())) for path in stale]
            print(f"dry-run: {len(paths)} file(s) would change; fetched with reconcile window")
            for path in paths:
                print(path)
            return 0
        written = write_outputs(args.root.resolve(), outputs)
        effective = "reconcile" if reconciled else "incremental"
        print(f"{effective}: {len(written)} file(s) changed")
        for path in written:
            print(path)
        return 0
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
