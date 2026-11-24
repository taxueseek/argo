"""Read-only X keyword search using the twitter-cli 0.8.5 JSON contract."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path

from engine_env import build_child_env

_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CANDIDATE_CAP = 100


def _instant(value: str, *, allow_date: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected an absolute ISO date or timestamp")
    if allow_date and _DATE.fullmatch(value):
        return datetime.combine(date.fromisoformat(value), day_time.min, timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires an explicit timezone")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("timestamp outside representable UTC range") from None


@dataclass(frozen=True)
class XConstraints:
    query: str
    author: str | None
    since: datetime | None
    until: datetime | None
    complex_query: bool

    @property
    def strict(self) -> bool:
        return bool(self.author or self.since or self.until)


def parse_x_constraints(query: str, since: str | None = None, until: str | None = None) -> XConstraints:
    """Extract only unambiguous positive operators; bare dates mean UTC midnight."""
    lower = _instant(since, allow_date=True) if since else None
    upper = _instant(until, allow_date=True) if until else None
    terms, ops = [], {}
    complex_query = False
    for token in query.split():
        if any(ch in token for ch in "()\"'\\|"):
            complex_query = True
            break
        key, sep, value = token.partition(":")
        key = key.lower()
        if sep and key in {"from", "since", "until"} and key not in ops:
            if key != "from" or _HANDLE.fullmatch(value.removeprefix("@")):
                ops[key] = value
                continue
        if not re.fullmatch(r"[#@]?\w+(?:[.-]\w+)*", token) or token.upper() in {"OR", "AND", "NOT"}:
            complex_query = True
            break
        terms.append(token)
    if not complex_query:
        if "since" in ops:
            bound = _instant(ops["since"], allow_date=True)
            lower = max(lower, bound) if lower else bound
        if "until" in ops:
            bound = _instant(ops["until"], allow_date=True)
            upper = min(upper, bound) if upper else bound
    if lower and upper and lower >= upper:
        raise ValueError("since must precede until")
    if complex_query:
        return XConstraints(query, None, lower, upper, True)
    return XConstraints(" ".join(terms), ops.get("from", "").removeprefix("@") or None, lower, upper, False)


def matches_x_constraints(c: XConstraints, screen_name: str | None, created_at_iso: str | None) -> bool:
    if c.author and (not isinstance(screen_name, str) or c.author.lower() != screen_name.lower()):
        return False
    if c.since or c.until:
        try:
            instant = _instant(created_at_iso)
        except ValueError:
            return False
        return (not c.since or c.since <= instant) and (not c.until or instant < c.until)
    return True


def _args(binary, c, count):
    args = [binary, "search", "--json", "--type", "latest", "--max", str(count)]
    if c.author:
        args += ["--from", c.author]
    # X day operators can omit posts near a UTC boundary. Include adjacent days
    # upstream; only createdAtISO determines membership in the requested window.
    if c.since and c.since.date() > date.min:
        args += ["--since", (c.since.date() - timedelta(days=1)).isoformat()]
    if c.until:
        day = c.until.date()
        pad = timedelta(days=1 if c.until.time() == day_time.min else 2)
        if day <= date.max - pad:
            args += ["--until", (day + pad).isoformat()]
    if c.query:
        args += ["--", c.query]
    return args


def _error(message, kind="error"):
    return [{"source": "twitter_cli", "error": message, "error_kind": kind}]


def _build_twitter_cli_engine(spec):
    def search(query, n=5, timeout=None, *, since=None, until=None, **kwargs):
        deadline = time.monotonic() + (spec.get("timeout", 20) if timeout is None else timeout)
        try:
            c = parse_x_constraints(query, since, until)
        except ValueError:
            return _error("invalid absolute X search window")
        binary = shutil.which("twitter")
        if not binary or Path(binary).suffix.lower() in {".cmd", ".bat"}:
            return _error("twitter-cli executable unavailable", "unavailable")
        # v0.8.5 caches both tweet lists and transaction data under Path.home().
        # A regular file as child home makes both cache paths non-writable.
        home = str(Path(binary).resolve())
        env = build_child_env("twitter_cli", spec, extra_env={
            "HOME": home, "USERPROFILE": home, "PYTHONDONTWRITEBYTECODE": "1",
        })
        if not (env.get("TWITTER_AUTH_TOKEN") and env.get("TWITTER_CT0")):
            return _error("twitter-cli credentials unavailable", "unavailable")
        target = max(1, min(int(n), _CANDIDATE_CAP))
        count = target
        results, seen = [], set()
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _error("twitter-cli search timed out")
            try:
                response = subprocess.run(_args(binary, c, count), env=env, timeout=remaining,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, check=False)
            except FileNotFoundError:
                return _error("twitter-cli executable unavailable", "unavailable")
            except subprocess.TimeoutExpired:
                return _error("twitter-cli search timed out")
            except OSError:
                return _error("twitter-cli could not execute search")
            if response.returncode:
                return _error("twitter-cli search failed")
            try:
                payload = json.loads(response.stdout)
            except (ValueError, UnicodeError):
                return _error("invalid twitter-cli JSON")
            if not (isinstance(payload, dict) and payload.get("ok") is True
                    and payload.get("schema_version") == "1" and isinstance(payload.get("data"), list)):
                return _error("invalid twitter-cli search response")
            batch = payload["data"]
            for item in batch:
                if not isinstance(item, dict) or not isinstance(item.get("author"), dict):
                    return _error("invalid twitter-cli tweet record")
                handle = item["author"].get("screenName")
                created, tid, text = item.get("createdAtISO"), item.get("id"), item.get("text")
                if not isinstance(tid, str) or not re.fullmatch(r"[0-9]{1,20}", tid) or not isinstance(text, str):
                    return _error("invalid twitter-cli tweet record")
                if tid in seen or not matches_x_constraints(c, handle, created):
                    continue
                if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
                    return _error("invalid twitter-cli tweet author")
                seen.add(tid)
                results.append({"source": "twitter_cli", "tweet_id": tid, "author": handle,
                                "url": f"https://x.com/{handle}/status/{tid}", "published_at": created,
                                "title": f"@{handle}: {text[:100]}", "summary": text, "snippet": text})
                if len(results) == target:
                    return results
            if attempt or not c.strict or len(batch) < count or count >= _CANDIDATE_CAP:
                break
            count = min(_CANDIDATE_CAP, max(10, count * 2))
        return results
    return search
