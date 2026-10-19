"""Local SQLite state: deploy ledger, eval records, serving log.

Append-only by convention: no UPDATE/DELETE statements exist in this module
(except ``prune_serving``, which is an explicit, user-invoked retention action).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from promptpin.config import db_path, ensure_state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS deploys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    env           TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    ref           TEXT NOT NULL,
    resolved_sha  TEXT NOT NULL,
    previous_sha  TEXT,
    actor         TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    mutable_ref   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS evals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    prompt        TEXT NOT NULL,
    ref           TEXT NOT NULL,
    resolved_sha  TEXT NOT NULL,
    suite         TEXT NOT NULL,
    dataset_sha   TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL NOT NULL,
    recorded_by   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS serving (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    env           TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    sha           TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    request_id    TEXT NOT NULL DEFAULT '',
    latency_ms    REAL,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deploys_env_prompt ON deploys (env, prompt, id);
CREATE INDEX IF NOT EXISTS idx_evals_prompt_sha ON evals (prompt, resolved_sha);
CREATE INDEX IF NOT EXISTS idx_serving_prompt_env_ts ON serving (prompt, env, ts);
"""


def connect(repo_root: Path) -> sqlite3.Connection:
    ensure_state_dir(repo_root)
    conn = sqlite3.connect(db_path(repo_root))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def utcnow() -> float:
    return time.time()


# ---------------------------------------------------------------- deploys ---


def record_deploy(
    conn: sqlite3.Connection,
    *,
    env: str,
    prompt: str,
    ref: str,
    resolved_sha: str,
    previous_sha: str | None,
    actor: str,
    note: str = "",
    mutable_ref: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO deploys (ts, env, prompt, ref, resolved_sha, previous_sha,"
        " actor, note, mutable_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            env,
            prompt,
            ref,
            resolved_sha,
            previous_sha,
            actor,
            note,
            1 if mutable_ref else 0,
        ),
    )
    conn.commit()
    return cur.lastrowid


def deploy_history(
    conn: sqlite3.Connection,
    env: str | None = None,
    prompt: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM deploys WHERE 1=1"
    args: list = []
    if env:
        query += " AND env = ?"
        args.append(env)
    if prompt:
        query += " AND prompt = ?"
        args.append(prompt)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(query, args).fetchall()


def previous_pin(conn: sqlite3.Connection, env: str, prompt: str) -> sqlite3.Row | None:
    """The deploy entry before the current one for (env, prompt), if any."""
    rows = conn.execute(
        "SELECT * FROM deploys WHERE env = ? AND prompt = ? ORDER BY id DESC LIMIT 2",
        (env, prompt),
    ).fetchall()
    return rows[1] if len(rows) == 2 else None


# ----------------------------------------------------------------- evals ---


def record_eval(
    conn: sqlite3.Connection,
    *,
    prompt: str,
    ref: str,
    resolved_sha: str,
    suite: str,
    dataset_sha: str,
    metric: str,
    value: float,
    recorded_by: str,
    note: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO evals (ts, prompt, ref, resolved_sha, suite, dataset_sha,"
        " metric, value, recorded_by, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utcnow(), prompt, ref, resolved_sha, suite, dataset_sha, metric, value, recorded_by, note),
    )
    conn.commit()
    return cur.lastrowid


def evals_for_sha(conn: sqlite3.Connection, prompt: str, sha: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evals WHERE prompt = ? AND resolved_sha = ? ORDER BY ts DESC",
        (prompt, sha),
    ).fetchall()


def evals_for_prompt(
    conn: sqlite3.Connection, prompt: str, ref: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    query = "SELECT * FROM evals WHERE prompt = ?"
    args: list = [prompt]
    if ref:
        query += " AND ref = ?"
        args.append(ref)
    query += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return conn.execute(query, args).fetchall()


def latest_eval_values(conn: sqlite3.Connection, prompt: str, sha: str) -> dict[str, dict]:
    """Latest recorded value per (metric, suite, dataset_sha) for a version.

    Returns {metric: {"value": float, "suite": str, "dataset_sha": str,
    "recorded_by": str, "ts": float, "n_records": int}}.
    Keeping suite+dataset in the key grouping surfaces cherry-picking:
    two suites disagreeing on the same metric both show up.
    """
    rows = evals_for_sha(conn, prompt, sha)
    latest: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r["metric"], r["suite"], r["dataset_sha"])
        if key not in latest:
            latest[key] = {
                "value": r["value"],
                "suite": r["suite"],
                "dataset_sha": r["dataset_sha"],
                "recorded_by": r["recorded_by"],
                "ts": r["ts"],
                "n_records": 0,
            }
        latest[key]["n_records"] += 1
    # Collapse to one entry per metric, preferring the most recent; if two
    # provenances disagree the caller still sees both via evals_for_sha.
    by_metric: dict[str, dict] = {}
    for (metric, _suite, _ds), info in sorted(
        latest.items(), key=lambda kv: kv[1]["ts"], reverse=True
    ):
        by_metric.setdefault(metric, info)
    return by_metric


# --------------------------------------------------------------- serving ---


def record_serve(
    conn: sqlite3.Connection,
    *,
    env: str,
    prompt: str,
    sha: str,
    model: str = "",
    request_id: str = "",
    latency_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO serving (ts, env, prompt, sha, model, request_id,"
        " latency_ms, tokens_in, tokens_out, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utcnow(), env, prompt, sha, model, request_id, latency_ms, tokens_in, tokens_out, error),
    )
    conn.commit()
    return cur.lastrowid


def serving_stats(
    conn: sqlite3.Connection,
    prompt: str | None = None,
    env: str | None = None,
    since_ts: float | None = None,
) -> dict:
    query = (
        "SELECT sha, model, COUNT(*) AS n,"
        " AVG(latency_ms) AS avg_latency_ms,"
        " SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS n_errors"
        " FROM serving WHERE 1=1"
    )
    args: list = []
    if prompt:
        query += " AND prompt = ?"
        args.append(prompt)
    if env:
        query += " AND env = ?"
        args.append(env)
    if since_ts:
        query += " AND ts >= ?"
        args.append(since_ts)
    query += " GROUP BY sha, model ORDER BY n DESC"
    rows = conn.execute(query, args).fetchall()
    total = sum(r["n"] for r in rows)
    return {
        "total": total,
        "versions": [
            {
                "sha": r["sha"],
                "model": r["model"],
                "count": r["n"],
                "share": (r["n"] / total) if total else 0.0,
                "avg_latency_ms": r["avg_latency_ms"],
                "error_rate": (r["n_errors"] / r["n"]) if r["n"] else 0.0,
            }
            for r in rows
        ],
    }


def prune_serving(conn: sqlite3.Connection, older_than_ts: float) -> int:
    cur = conn.execute("DELETE FROM serving WHERE ts < ?", (older_than_ts,))
    conn.commit()
    return cur.rowcount
