"""``promptpin log verify``: did production serve only what was pinned?

Compares the serving log against the committed lock. For every pinned
``(env, prompt)`` it looks at requests served *after the current pin went
live* (plus a grace period for rolling restarts) and reports any that used a
different commit or config digest. The log is written by the application, so
this proves what the application *recorded*; pair it with ``Receipt.log()``,
whose values come from bytes the runtime verified.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from promptpin import deploys, gitutil
from promptpin.deploys import Check
from promptpin.errors import PromptpinError


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _check_grace_window(conn, checks, top, env, name, live_at, start) -> None:
    """During the grace period old pins may still serve, but only commits that were pinned once."""
    if start <= live_at:
        return
    rows = conn.execute(
        "SELECT sha, COUNT(*) AS n FROM serving WHERE env = ? AND prompt = ?"
        " AND ts >= ? AND ts < ? GROUP BY sha",
        (env, name, live_at, start),
    ).fetchall()
    if not rows:
        return
    from promptpin import history

    try:
        known, _ = history.known_shas(top, env, name)
    except PromptpinError:
        known = None
    if known is None:
        return  # no committed history to compare with (e.g. --no-commit deploys)
    for r in rows:
        if r["sha"] not in known:
            checks.append(
                Check(
                    "error",
                    env,
                    name,
                    f"{r['n']} request(s) in the grace period served {str(r['sha'])[:7]},"
                    " a commit that was never pinned here",
                )
            )


def verify_serving(
    repo_root: Path,
    env: str,
    db_file: Path | None = None,
    *,
    grace_seconds: float = 300.0,
    since_ts: float | None = None,
) -> list[Check]:
    """Checks for the serving log of ``env``. Fails closed on any setup problem."""
    from promptpin.config import db_path

    top = gitutil.ensure_repo(repo_root)
    try:
        pins = deploys.read_lock(top).get(env) or {}
    except Exception as exc:  # noqa: BLE001 - reported as a check, not a traceback
        return [Check("error", env, "-", f"lock file unreadable: {exc}")]
    if not pins:
        return [Check("error", env, "-", f"no pins for env {env!r} in the lock file")]
    path = Path(db_file) if db_file else db_path(top)
    if not path.exists():
        return [Check("error", env, "-", f"serving log {path.name} not found — nothing to verify")]
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(serving)")}
    except sqlite3.Error as exc:
        return [Check("error", env, "-", f"serving log unreadable: {exc}")]
    if not cols:
        return [Check("error", env, "-", "serving log has no 'serving' table")]
    has_digest = "digest" in cols
    checks: list[Check] = []
    floor = None
    try:
        for name, pin in sorted(pins.items()):
            live_at = _parse_ts(pin.deployed_at)
            if live_at is None:
                checks.append(
                    Check("error", env, name, f"pin has no valid deployed_at ({pin.deployed_at!r})")
                )
                continue
            start = live_at + grace_seconds
            _check_grace_window(conn, checks, top, env, name, live_at, start)
            if since_ts is not None:
                start = max(start, since_ts)
            floor = start if floor is None else min(floor, start)
            select = "sha, " + ("digest" if has_digest else "'' AS digest") + ", COUNT(*) AS n"
            rows = conn.execute(
                f"SELECT {select} FROM serving WHERE env = ? AND prompt = ? AND ts >= ?"
                " GROUP BY sha, 2",
                (env, name, start),
            ).fetchall()
            total = sum(r["n"] for r in rows)
            if total == 0:
                checks.append(
                    Check(
                        "warning",
                        env,
                        name,
                        "no requests logged since the pin went live"
                        f" (the first {grace_seconds:g}s only count for never-pinned commits)",
                    )
                )
                continue
            for r in rows:
                if r["sha"] != pin.sha:
                    checks.append(
                        Check(
                            "error",
                            env,
                            name,
                            f"{r['n']} of {total} request(s) served {str(r['sha'])[:7]},"
                            f" not the pinned {pin.sha[:7]}",
                        )
                    )
                elif r["digest"] and pin.digest and r["digest"] != pin.digest:
                    checks.append(
                        Check(
                            "error",
                            env,
                            name,
                            f"{r['n']} request(s) at the pinned commit used a different"
                            " config digest (model/params/format drift)",
                        )
                    )
                elif not r["digest"]:
                    checks.append(
                        Check(
                            "warning",
                            env,
                            name,
                            f"{r['n']} request(s) logged without a digest — use Receipt.log()",
                        )
                    )
        pinned = set(pins)
        for r in conn.execute(
            "SELECT prompt, COUNT(*) AS n FROM serving WHERE env = ? AND ts >= ? GROUP BY prompt",
            (env, floor if floor is not None else 0),
        ):
            if r["prompt"] not in pinned:
                checks.append(
                    Check(
                        "error",
                        env,
                        r["prompt"],
                        f"{r['n']} request(s) served a prompt that is not pinned in {env!r}",
                    )
                )
    except sqlite3.Error as exc:
        checks.append(Check("error", env, "-", f"serving log query failed: {exc}"))
    finally:
        conn.close()
    return checks
