"""promptpin CLI — git-native prompt registry."""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from promptpin import deploys, gitutil, registry, statedb
from promptpin import diff as diffmod
from promptpin import validate as validatemod
from promptpin.config import (
    CONFIG_FILENAME,
    LOCK_FILENAME,
    STATE_DIRNAME,
    Config,
)
from promptpin.errors import PromptpinError


def _repo(args) -> Path:
    return gitutil.ensure_repo(Path(args.repo or Path.cwd()))


def _parse_name_ref(spec: str) -> tuple[str, str]:
    if "@" not in spec:
        raise PromptpinError(f"{spec!r} needs a version: <name>@<v3|sha|branch|latest>")
    name, ref = spec.split("@", 1)
    if not name or not ref:
        raise PromptpinError(f"could not parse {spec!r}: expected <name>@<ref>")
    return name, ref


# ------------------------------------------------------------------ init ---


def cmd_init(args) -> int:
    cwd = Path(args.repo or Path.cwd())
    top = gitutil.ensure_repo(cwd)
    cfg_path = top / CONFIG_FILENAME
    if not cfg_path.exists():
        cfg = Config()
        cfg_path.write_text(cfg.dump(), encoding="utf-8")
        print(f"wrote {CONFIG_FILENAME}")
    prompts_dir = top / Config.load(top).prompts_dir
    prompts_dir.mkdir(parents=True, exist_ok=True)
    example = prompts_dir / "greeter.yaml"
    if not any(prompts_dir.iterdir()):
        example.write_text(
            "name: greeter\n"
            "description: Example prompt — edit me, commit, then release + deploy.\n"
            "model: gpt-4o-mini\n"
            "params:\n"
            "  temperature: 0.7\n"
            "variables:\n"
            "  - user_name\n"
            "template: |\n"
            "  Greet {{user_name}} warmly in one short sentence.\n",
            encoding="utf-8",
        )
        print(f"wrote example {example.relative_to(top)}")
    gitignore = top / ".gitignore"
    entry = f"{STATE_DIRNAME}/\n"
    if gitignore.exists():
        if entry not in gitignore.read_text(encoding="utf-8"):
            with open(gitignore, "a", encoding="utf-8") as fh:
                fh.write(entry)
            print(f"added {STATE_DIRNAME}/ to .gitignore")
    else:
        gitignore.write_text(entry, encoding="utf-8")
        print("wrote .gitignore")
    print("\nnext steps:")
    print("  1. edit prompts/greeter.yaml, then: git add -A && git commit -m 'add greeter'")
    print("  2. promptpin release greeter            # tags promptpin/greeter@v1")
    print("  3. promptpin deploy greeter@v1 --env staging")
    return 0


# ------------------------------------------------------------------ list ---


def cmd_list(args) -> int:
    top = _repo(args)
    cfg = Config.load(top)
    names = registry.list_prompts(top, cfg.prompts_dir)
    if not names:
        print(f"no prompts in {cfg.prompts_dir}/ yet — run `promptpin init`")
        return 0
    for name in names:
        try:
            prompt = registry.load_prompt(top, cfg.prompts_dir, name)
            latest = gitutil.latest_tag(top, name)
            print(f"{name:24} {prompt.model or '(no model)':20} latest={latest or '(unreleased)'}")
        except PromptpinError as exc:
            print(f"{name:24} ERROR: {exc}")
    return 0


# --------------------------------------------------------------- versions ---


def cmd_versions(args) -> int:
    top = _repo(args)
    tags = gitutil.list_version_tags(top, args.name)
    if not tags:
        print(f"no released versions of {args.name!r} — run `promptpin release {args.name}`")
        return 0
    for tag in tags:
        sha, _ = gitutil.resolve_ref(top, tag)
        print(f"{tag.rsplit('@', 1)[-1]:6} {tag:40} {sha[:7]}")
    return 0


# ------------------------------------------------------------------ show ---


def cmd_show(args) -> int:
    top = _repo(args)
    name, ref = _parse_name_ref(args.spec) if "@" in args.spec else (args.spec, "latest")
    prompt, sha, display = diffmod.load_at(top, name, ref)
    variables = dict(kv.split("=", 1) for kv in (args.var or []))
    template = prompt.template
    if variables:
        from promptpin.client import render_template

        try:
            template = render_template(template, variables)
        except KeyError as exc:
            raise PromptpinError(str(exc)) from exc
    print(f"# {prompt.name} @ {display} ({sha[:7]})")
    if prompt.description:
        print(f"# {prompt.description}")
    print(f"# model={prompt.model or '(unset)'} params={prompt.params or {}}")
    print()
    print(template)
    return 0


# ------------------------------------------------------------------ diff ---


def cmd_diff(args) -> int:
    top = _repo(args)
    print(diffmod.render_diff(top, args.name, args.a, args.b, color=args.color))
    return 0


# -------------------------------------------------------------- validate ---


def cmd_validate(args) -> int:
    top = _repo(args)
    findings = validatemod.validate_all(top)
    if not findings:
        print("all prompts valid")
        return 0
    for finding in findings:
        print(finding)
    if not validatemod.is_clean(findings):
        print("\nvalidation FAILED (errors above)")
        return 1
    print("\nvalid with warnings")
    return 0


# --------------------------------------------------------------- release ---


def cmd_release(args) -> int:
    top = _repo(args)
    cfg = Config.load(top)
    prompt = registry.load_prompt(top, cfg.prompts_dir, args.name)
    # Footgun guard: tagging HEAD while the prompt file has uncommitted edits
    # would release *without* those edits. Warn loudly.
    rel = registry.prompt_relpath(top, cfg.prompts_dir, args.name) or ""
    dirty = [line[3:] for line in gitutil.status_porcelain(top).splitlines() if len(line) > 3]
    if rel and rel in dirty:
        print(
            f"⚠ warning: {rel} has uncommitted changes — this release will NOT include them.\n"
            "  Commit first, or the tag will point at the old version."
        )
    sha = gitutil.head_sha(top) if not args.sha else gitutil.resolve_ref(top, args.sha)[0]
    # The prompt file must exist at the tagged commit.
    rel = None
    for ext in (".yaml", ".yml", ".md"):
        candidate = f"{cfg.prompts_dir}/{args.name}{ext}"
        try:
            gitutil.show_file(top, sha, candidate)
            rel = candidate
            break
        except Exception:
            continue
    if rel is None:
        raise PromptpinError(
            f"prompt {args.name!r} has no file in {cfg.prompts_dir}/ at {sha[:7]} — "
            "commit the prompt file first"
        )
    version = args.as_ or gitutil.next_version(top, args.name)
    tag = gitutil.tag_name(args.name, version)
    existing = gitutil.list_version_tags(top, args.name)
    if tag in existing:
        raise PromptpinError(f"{tag} already exists — pass --as vN for a new version")
    message = args.message or f"release {args.name} {version}"
    gitutil.create_tag(top, args.name, version, sha, message)
    print(f"released {args.name}@{version} → {tag} ({sha[:7]})")
    if prompt.model:
        print(f"  model={prompt.model} params={prompt.params or {}}")
    print(f"deploy it: promptpin deploy {args.name}@{version} --env <env>")
    return 0


# ---------------------------------------------------------------- deploy ---


def cmd_deploy(args) -> int:
    top = _repo(args)
    name, ref = _parse_name_ref(args.spec)
    result = deploys.deploy(
        top,
        name,
        ref,
        args.env,
        note=args.note or "",
        commit=(not args.no_commit),
    )
    print(f"pinned {name}@{result['display_ref']} → {sha7(result['sha'])} on {args.env}")
    if result["previous_sha"]:
        print(f"  previous: {sha7(result['previous_sha'])}")
    else:
        print("  previous: (none — first deploy)")
    if result["model"]:
        print(f"  model={result['model']}")
    if result["mutable_ref"]:
        print("  ⚠ MUTABLE REF: the ref can move — the pin is frozen at the SHA above.")
        print("    Prefer tags/SHAs for production deploys.")
    if result["commit_sha"]:
        print(f"  committed {LOCK_FILENAME} as {sha7(result['commit_sha'])}")
    else:
        print(f"  (not committed — {LOCK_FILENAME} has uncommitted changes)")
    return 0


def sha7(sha: str) -> str:
    return sha[:7]


# -------------------------------------------------------------- rollback ---


def cmd_rollback(args) -> int:
    top = _repo(args)
    result = deploys.rollback(top, args.name, args.env, note=args.note or "")
    print(f"rolled back {args.name} on {args.env} → {sha7(result['sha'])}")
    print(f"  (was {sha7(result['rolled_back_from'])})")
    if result["commit_sha"]:
        print(f"  committed {LOCK_FILENAME} as {sha7(result['commit_sha'])}")
    return 0


# ---------------------------------------------------------------- status ---


def cmd_status(args) -> int:
    top = _repo(args)
    pins = deploys.read_lock(top)
    if not pins:
        print(f"nothing deployed yet — {LOCK_FILENAME} is empty or missing")
        return 0
    envs = [args.env] if args.env else sorted(pins)
    for env in envs:
        prompts = pins.get(env, {})
        if not prompts:
            continue
        print(f"## {env}")
        for name in sorted(prompts):
            pin = prompts[name]
            flag = " ⚠MUTABLE" if pin.mutable_ref else ""
            print(
                f"  {name:24} {pin.ref:12} {sha7(pin.sha)}"
                f"  {pin.model or '(no model)':18} by {pin.deployed_by} {pin.deployed_at}{flag}"
            )
    issues = deploys.status_issues(top, pins)
    if issues:
        print("\nissues:")
        for issue in issues:
            print(f"  ⚠ {issue}")
    return 0


# --------------------------------------------------------------- history ---


def cmd_history(args) -> int:
    top = _repo(args)
    conn = statedb.connect(top)
    try:
        rows = statedb.deploy_history(conn, env=args.env, prompt=args.prompt, limit=args.limit)
    finally:
        conn.close()
    if not rows:
        print("no deploys recorded in the local ledger")
        return 0
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        mut = " ⚠MUTABLE" if r["mutable_ref"] else ""
        prev = f" (was {r['previous_sha'][:7]})" if r["previous_sha"] else ""
        note = f" — {r['note']}" if r["note"] else ""
        print(
            f"{ts}  {r['env']:12} {r['prompt']:20} {r['ref']:24}"
            f" → {r['resolved_sha'][:7]}{prev} by {r['actor']}{mut}{note}"
        )
    return 0


# ------------------------------------------------------------------ lock ---


def cmd_lock(args) -> int:
    """Export a minimal runtime manifest: env → prompt → {sha, model, params, template_sha256}."""
    import yaml

    top = _repo(args)
    pins = deploys.read_lock(top)
    prompts = pins.get(args.env, {})
    if not prompts:
        raise PromptpinError(f"no pins for env {args.env!r}")
    manifest = {
        "env": args.env,
        "prompts": {
            name: {
                "sha": pin.sha,
                "model": pin.model,
                "params": pin.params,
                "template_sha256": pin.template_sha256,
            }
            for name, pin in prompts.items()
        },
    }
    out = Path(args.out)
    out.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    print(f"wrote {out} ({len(prompts)} prompts for {args.env})")
    return 0


def cmd_export(args) -> int:
    top = _repo(args)
    result = deploys.export_bundle(top, args.env, Path(args.out))
    print(f"exported {len(result['prompts'])} prompts for {args.env} → {result['out']}/")
    print("bundle with your app; load with promptpin.load_bundle(<dir>)")
    return 0


# ------------------------------------------------------------------- log ---


def _parse_duration(s: str) -> float:
    import time as _time

    now = _time.time()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units and s[:-1].isdigit():
        return now - int(s[:-1]) * units[s[-1]]
    raise PromptpinError(f"could not parse duration {s!r} — use e.g. 24h, 7d, 30m")


def cmd_log(args) -> int:
    top = _repo(args)
    if args.log_cmd == "serve":
        conn = statedb.connect(top)
        try:
            statedb.record_serve(
                conn,
                env=args.env,
                prompt=args.prompt,
                sha=args.sha,
                model=args.model or "",
                request_id=args.request_id or "",
                latency_ms=args.latency_ms,
                tokens_in=args.tokens_in,
                tokens_out=args.tokens_out,
                error=args.error or "",
            )
        finally:
            conn.close()
        print("logged")
        return 0
    if args.log_cmd == "stats":
        conn = statedb.connect(top)
        try:
            since = _parse_duration(args.since) if args.since else None
            stats = statedb.serving_stats(conn, prompt=args.prompt, env=args.env, since_ts=since)
        finally:
            conn.close()
        print(f"serving events: {stats['total']}")
        for v in stats["versions"]:
            lat = f"{v['avg_latency_ms']:.1f}ms" if v["avg_latency_ms"] is not None else "n/a"
            print(
                f"  {v['sha'][:7]}  {v['model'] or '(no model)':18}"
                f"  n={v['count']} ({v['share']:.0%})  avg_latency={lat}"
                f"  error_rate={v['error_rate']:.1%}"
            )
        return 0
    if args.log_cmd == "prune":
        conn = statedb.connect(top)
        try:
            cutoff = _parse_duration(args.older_than)
            n = statedb.prune_serving(conn, cutoff)
        finally:
            conn.close()
        print(f"pruned {n} serving events older than {args.older_than}")
        return 0
    raise PromptpinError(f"unknown log subcommand {args.log_cmd!r}")


# ------------------------------------------------------------------ eval ---


def cmd_eval(args) -> int:
    top = _repo(args)
    if args.eval_cmd == "record":
        metrics = {}
        for item in args.metric or []:
            if "=" not in item:
                raise PromptpinError(f"bad --metric {item!r}: expected name=value")
            k, v = item.split("=", 1)
            try:
                metrics[k] = float(v)
            except ValueError as exc:
                raise PromptpinError(f"bad --metric {item!r}: value must be numeric") from exc
        if not metrics:
            raise PromptpinError("at least one --metric name=value is required")
        if not args.suite:
            raise PromptpinError("--suite is required (eval provenance)")
        if not args.dataset_sha:
            raise PromptpinError("--dataset-sha is required (eval provenance)")
        sha, display, _ = gitutil.resolve_prompt_ref(top, args.prompt, args.ref)
        conn = statedb.connect(top)
        try:
            for metric, value in metrics.items():
                statedb.record_eval(
                    conn,
                    prompt=args.prompt,
                    ref=args.ref,
                    resolved_sha=sha,
                    suite=args.suite,
                    dataset_sha=args.dataset_sha,
                    metric=metric,
                    value=value,
                    recorded_by=gitutil.current_actor(top),
                    note=args.note or "",
                )
        finally:
            conn.close()
        print(
            f"recorded {len(metrics)} metric(s) for {args.prompt}@{display}"
            f" ({sha[:7]}) from suite {args.suite!r}"
        )
        return 0
    raise PromptpinError(f"unknown eval subcommand {args.eval_cmd!r}")


def cmd_evals(args) -> int:
    top = _repo(args)
    conn = statedb.connect(top)
    try:
        rows = statedb.evals_for_prompt(conn, args.name, ref=args.ref)
    finally:
        conn.close()
    if not rows:
        print(f"no evals recorded for {args.name!r}")
        return 0
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        note = f" — {r['note']}" if r["note"] else ""
        print(
            f"{ts}  {r['ref']:14} {r['resolved_sha'][:7]}"
            f"  {r['metric']}={r['value']:.4g}"
            f"  suite={r['suite']} dataset={r['dataset_sha'][:7]}"
            f"  by {r['recorded_by']}{note}"
        )
    return 0


# ----------------------------------------------------------------- audit ---


def cmd_audit(args) -> int:
    top = _repo(args)
    cfg = Config.load(top)
    names = [args.prompt] if args.prompt else registry.list_prompts(top, cfg.prompts_dir)
    for name in names:
        rel = registry.prompt_relpath(top, cfg.prompts_dir, name)
        if rel is None:
            continue
        print(f"## {name} ({rel})")
        for entry in gitutil.file_history(top, rel, limit=10):
            print(
                f"  {entry['date'][:10]} {entry['sha'][:7]} {entry['author']:20} {entry['message']}"
            )
    conn = statedb.connect(top)
    try:
        rows = statedb.deploy_history(conn, prompt=args.prompt, limit=10)
    finally:
        conn.close()
    if rows:
        print("\n## recent deploys")
        for r in rows:
            ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
                "%Y-%m-%d"
            )
            print(
                f"  {ts} {r['env']:12} {r['prompt']:20} {r['ref']:20}"
                f" → {r['resolved_sha'][:7]} by {r['actor']}"
            )
    return 0


# ----------------------------------------------------------------- parser ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="promptpin",
        description="Git-native prompt registry: version prompts with git, "
        "pin versions to envs, log what served, diff with evals attached.",
    )
    p.add_argument("--repo", help="repo path (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scaffold promptpin.yaml, prompts/, .gitignore")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("list", help="list prompts with latest released version")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("versions", help="list released versions of a prompt")
    s.add_argument("name")
    s.set_defaults(func=cmd_versions)

    s = sub.add_parser("show", help="show a prompt at a version (default: latest)")
    s.add_argument("spec", help="<name>[@<v3|sha|branch|latest>]")
    s.add_argument("--var", action="append", help="template variable as k=v (repeatable)")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("diff", help="three-panel diff: template, config, recorded evals")
    s.add_argument("name")
    s.add_argument("a", help="older ref (v3, sha, ...)")
    s.add_argument("b", help="newer ref")
    s.add_argument("--color", action="store_true")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("validate", help="schema + variable + best-effort secret checks")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("release", help="tag the current (or given) commit as a version")
    s.add_argument("name")
    s.add_argument("--as", dest="as_", help="version label, e.g. v4 (default: auto-increment)")
    s.add_argument("--sha", help="commit to tag (default: HEAD)")
    s.add_argument("-m", "--message", help="tag message")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("deploy", help="pin <name>@<ref> to an env")
    s.add_argument("spec", help="<name>@<v3|sha|branch|latest>")
    s.add_argument("--env", required=True)
    s.add_argument("--note", default="")
    s.add_argument("--no-commit", action="store_true", help="don't commit promptpin.lock")
    s.set_defaults(func=cmd_deploy)

    s = sub.add_parser("rollback", help="redeploy the previous pin for an env")
    s.add_argument("name")
    s.add_argument("--env", required=True)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("status", help="show what's pinned where")
    s.add_argument("--env")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("history", help="deploy ledger")
    s.add_argument("--env")
    s.add_argument("--prompt")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("lock", help="export a minimal runtime manifest for an env")
    s.add_argument("--env", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser(
        "export",
        help="export pinned templates + manifest as a runtime bundle (no git needed at runtime)",
    )
    s.add_argument("--env", required=True)
    s.add_argument("--out", required=True, help="bundle directory to write")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("log", help="serving log")
    log_sub = s.add_subparsers(dest="log_cmd", required=True)
    ls = log_sub.add_parser("serve", help="record one serving event")
    ls.add_argument("--prompt", required=True)
    ls.add_argument("--sha", required=True)
    ls.add_argument("--env", default="production")
    ls.add_argument("--model", default="")
    ls.add_argument("--request-id", default="")
    ls.add_argument("--latency-ms", type=float)
    ls.add_argument("--tokens-in", type=int)
    ls.add_argument("--tokens-out", type=int)
    ls.add_argument("--error", default="")
    ls = log_sub.add_parser("stats", help="serving stats per version")
    ls.add_argument("--prompt")
    ls.add_argument("--env")
    ls.add_argument("--since", help="e.g. 24h, 7d, 30m")
    ls = log_sub.add_parser("prune", help="delete serving events older than a duration")
    ls.add_argument("--older-than", required=True, help="e.g. 30d")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("eval", help="eval scores")
    eval_sub = s.add_subparsers(dest="eval_cmd", required=True)
    er = eval_sub.add_parser("record", help="attach eval scores to a version")
    er.add_argument("--prompt", required=True)
    er.add_argument("--ref", required=True)
    # --suite / --dataset-sha are validated in cmd_eval (not argparse-required)
    # so the failure is a friendly promptpin error (exit 1), not argparse exit 2.
    er.add_argument("--suite", default=None, help="eval suite name (provenance, required)")
    er.add_argument("--dataset-sha", default=None, help="eval dataset hash (provenance, required)")
    er.add_argument("--metric", action="append", help="name=value (repeatable)")
    er.add_argument("--note", default="")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("evals", help="show recorded evals for a prompt")
    s.add_argument("name")
    s.add_argument("--ref")
    s.set_defaults(func=cmd_evals)

    s = sub.add_parser("audit", help="who changed what: git history + deploy ledger")
    s.add_argument("--prompt")
    s.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PromptpinError as exc:
        print(f"promptpin: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
