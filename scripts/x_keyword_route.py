"""X keyword channels share a bounded deadline and per-attempt search outcomes."""

from __future__ import annotations

import re
import subprocess
import time

import engine_states as st

X_ENGINES = frozenset({"twitter", "twitter_cli", "fxtwitter"})


def is_x_request(engine, query, decision):
    if engine != "auto":
        return engine in X_ENGINES
    from route_domains import platform_native_engine

    return (
        decision.get("domain") == "social"
        and platform_native_engine(query) == "fxtwitter"
        or bool(re.search(r"(?:^|\s)from:@?[A-Za-z0-9_]{1,15}(?:\s|$)", query, re.I))
    )


def _call(engine, query, n, timeout, since, until):
    from engines import get_registry

    handler = get_registry().get(engine)
    if handler is None:
        raise FileNotFoundError(f"{engine} not enabled or available")
    if engine == "twitter":
        from x_search import search_browser

        return search_browser(query, n, timeout)
    return handler(query, n, timeout=timeout, since=since, until=until)


def search_x_keywords(query, n=5, *, engine="auto", since=None, until=None, timeout=20):
    from engine_dispatch import classify_engine_outcome
    from engines_twitter_cli import parse_x_constraints, matches_x_constraints
    from time_utils import parse_time_value

    start = time.monotonic()

    def bound(value):
        if value and re.fullmatch(r"\d+\s*[dhw]|\d+", str(value), re.I):
            return parse_time_value(value)[0] or value
        return value

    since, until = bound(since), bound(until)
    attempts, errors, hits = [], [], []
    try:
        constraints = parse_x_constraints(query, since, until)
    except ValueError as exc:
        errors.append(f"X: invalid query window: {exc}")
        order = []
    else:
        order = (
            (
                ["twitter_cli", "fxtwitter", "twitter"]
                if constraints.strict
                else ["fxtwitter", "twitter_cli", "twitter"]
            )
            if engine == "auto"
            else [engine]
        )
    for name in order:
        before = time.monotonic()
        remaining = timeout - (before - start)
        if remaining <= 0:
            errors.append("X: search deadline exceeded")
            break
        try:
            channel_timeout = (
                min(10, remaining)
                if name == "fxtwitter" and engine == "auto"
                else remaining
            )
            rows = _call(name, query, n, channel_timeout, since, until)
        except FileNotFoundError as exc:
            rows = [{"error": str(exc), "error_kind": st.PLUGIN_UNAVAILABLE}]
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            rows = (
                [{"error": f"{name}: {type(exc).__name__}"}]
                if isinstance(exc, subprocess.TimeoutExpired)
                else [{"error": str(exc)}]
            )
        checked = []
        for row in rows:
            if "error" in row:
                checked.append(row)
                continue
            if not row.get("url"):
                checked.append({"error": "response row has no post URL"})
                continue
            author = row.get("author") or (row.get("social_meta") or {}).get("author")
            if name == "fxtwitter" and row.get("source") != "fxtwitter":
                author = row.get("source")
            published = row.get("published_at")
            if (constraints.author and not author) or (
                (constraints.since or constraints.until) and not published
            ):
                checked.append(
                    {
                        "error": "post lacks metadata needed to verify X search constraints"
                    }
                )
            elif matches_x_constraints(constraints, author, published):
                checked.append(row)
        outcome = classify_engine_outcome(
            name, checked, round((time.monotonic() - before) * 1000)
        )
        for row in checked:
            if "error" in row:
                kind = row.get("error_kind")
                if kind:
                    outcome["error_kind"] = kind
                errors.append(
                    f"{name}: {kind + ': ' if kind else ''}{str(row['error'])[:200]}"
                )
            else:
                hits.append(row)
        attempts.append(outcome)
        if hits or not rows or (checked == [] and engine != "auto"):
            break
    used = [o["engine"] for o in attempts]
    return {
        "query": query,
        "domain": "social",
        "engine": used[-1] if used else engine,
        "engines_used": used,
        "results": hits[:n],
        "count": len(hits[:n]),
        "errors": errors,
        "engine_outcomes": attempts,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
    }
