"""promptpin CLI — git-native prompt registry."""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from promptpin import __version__, deploys, fsutil, gitutil, registry, safety, statedb, yamlsafe
from promptpin import diff as diffmod
from promptpin import validate as validatemod
from promptpin.config import (
    CONFIG_FILENAME,
    LOCK_FILENAME,
    STATE_DIRNAME,
    Config,
)
from promptpin.errors import PromptpinError


def _github_annotation(level: str, title: str, message: str) -> str:
    """One GitHub Actions workflow command (``::error title=..::..``).

    Text comes from untrusted files, so it is made terminal-safe first and the
    characters that end or split a workflow command are percent-encoded, as the
    workflow-command syntax requires.
    """

    def enc(text: object, prop: bool) -> str:
        out = safety.one_line(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return out.replace(":", "%3A").replace(",", "%2C") if prop else out

    kind = "error" if level == "error" else "warning"
    return f"::{kind} title={enc(title, True)}::{enc(message, False)}"


def _say(text: object = "") -> None:
    """Print with control/bidi/zero-width characters made visible.

    Everything promptpin prints can contain untrusted text (prompt bodies,
    notes, commit messages, author names), so nothing reaches the terminal
    raw.
    """
    print(safety.sanitize(text))


_one = safety.one_line


def _label(name: str, display_ref: str) -> str:
    """``greeter@v1`` instead of ``greeter@promptpin/greeter@v1`` for release tags."""
    prefix = f"{gitutil.TAG_PREFIX}/{name}@"
    short = display_ref[len(prefix) :] if display_ref.startswith(prefix) else display_ref
    return f"{name}@{short}"


def _print_json(payload: object) -> None:
    import json

    print(json.dumps(payload, indent=2, ensure_ascii=True))


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
        fsutil.atomic_write_text(cfg_path, cfg.dump())
        _say(f"wrote {CONFIG_FILENAME}")
    prompts_dir = top / Config.load(top).prompts_dir
    prompts_dir.mkdir(parents=True, exist_ok=True)
    example = prompts_dir / "greeter.yaml"
    if not any(prompts_dir.iterdir()):
        fsutil.atomic_write_text(
            example,
            "name: greeter\n"
            "description: Example prompt — edit me, commit, then release + deploy.\n"
            "model: gpt-4o-mini\n"
            "params:\n"
            "  temperature: 0.7\n"
            "variables:\n"
            "  - user_name\n"
            "template: |\n"
            "  Greet {{user_name}} warmly in one short sentence.\n",
        )
        _say(f"wrote example {example.relative_to(top)}")
    gitignore = top / ".gitignore"
    entry = f"{STATE_DIRNAME}/\n"
    if gitignore.exists():
        if entry not in gitignore.read_text(encoding="utf-8"):
            old = gitignore.read_bytes()
            sep = b"" if old.endswith(b"\n") or not old else b"\n"
            fsutil.atomic_write_bytes(gitignore, old + sep + entry.encode("utf-8"))
            _say(f"added {STATE_DIRNAME}/ to .gitignore")
    else:
        fsutil.atomic_write_text(gitignore, entry)
        _say("wrote .gitignore")
    _say("\nnext steps:")
    _say("  1. edit prompts/greeter.yaml, then: git add -A && git commit -m 'add greeter'")
    _say("  2. promptpin release greeter            # tags promptpin/greeter@v1")
    _say("  3. promptpin deploy greeter@v1 --env staging")
    return 0


# ------------------------------------------------------------------ list ---


def cmd_list(args) -> int:
    top = _repo(args)
    cfg = Config.load(top)
    names = registry.list_prompts(top, cfg.prompts_dir)
    rows: list[dict] = []
    if not names and not args.json:
        _say(f"no prompts in {cfg.prompts_dir}/ yet — run `promptpin init`")
        return 0
    for name in names:
        try:
            prompt = registry.load_prompt(top, cfg.prompts_dir, name)
            latest = gitutil.latest_tag(top, name)
            rows.append({"name": name, "model": prompt.model, "latest": latest})
            if not args.json:
                _say(
                    f"{name:24} {prompt.model or '(no model)':20} latest={latest or '(unreleased)'}"
                )
        except PromptpinError as exc:
            rows.append({"name": name, "error": safety.one_line(exc)})
            if not args.json:
                _say(f"{name:24} ERROR: {exc}")
    if args.json:
        _print_json({"prompts": rows})
    return 0


# --------------------------------------------------------------- versions ---


def cmd_versions(args) -> int:
    top = _repo(args)
    tags = gitutil.list_version_tags(top, args.name)
    if not tags:
        _say(f"no released versions of {args.name!r} — run `promptpin release {args.name}`")
        return 0
    for tag in tags:
        sha, _ = gitutil.resolve_ref(top, tag)
        _say(f"{tag.rsplit('@', 1)[-1]:6} {tag:40} {sha[:7]}")
    return 0


# ------------------------------------------------------------------ show ---


def cmd_show(args) -> int:
    top = _repo(args)
    name, ref = _parse_name_ref(args.spec) if "@" in args.spec else (args.spec, "latest")
    prompt, sha, display = diffmod.load_at(top, name, ref)
    variables = {}
    for kv in args.var or []:
        key, sep, value = kv.partition("=")
        if not sep or not key:
            raise PromptpinError(f"bad --var {kv!r}: expected name=value")
        variables[key] = value
    _say(f"# {prompt.name} @ {display} ({sha[:7]})")
    if prompt.description:
        _say(f"# {_one(prompt.description)}")
    _say(f"# model={_one(prompt.model or '(unset)')} params={prompt.params or {}}")
    if prompt.format == "chat":
        _say("# format=chat")
    _say()
    from promptpin.client import render_messages, render_template

    try:
        if prompt.format == "chat":
            messages = (
                render_messages(prompt.template, variables)
                if variables
                else registry.split_messages(prompt.template)
            )
            for i, m in enumerate(messages):
                if i:
                    _say()
                _say(f"[{m['role']}]")
                _say(m["content"])
        else:
            _say(render_template(prompt.template, variables) if variables else prompt.template)
    except KeyError as exc:
        raise PromptpinError(str(exc).strip("'\"")) from exc
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
    ok = validatemod.is_clean(findings, strict=args.strict)
    if args.github:
        for f in findings:
            level = "error" if f.level == "error" or args.strict else "warning"
            print(_github_annotation(level, f"promptpin validate: {f.prompt}", f.message))
        if ok and not findings:
            _say("all prompts valid")
        return 0 if ok else 1
    if args.json:
        import json

        payload = {
            "ok": ok,
            "strict": args.strict,
            "findings": [
                {"level": f.level, "prompt": f.prompt, "message": f.message} for f in findings
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0 if ok else 1
    if not findings:
        _say("all prompts valid")
        return 0
    for finding in findings:
        _say(finding)
    if not ok:
        reason = "errors above" if not validatemod.is_clean(findings) else "--strict: warnings"
        _say(f"\nvalidation FAILED ({reason})")
        return 1
    _say("\nvalid with warnings")
    return 0


# ---------------------------------------------------------------- verify ---


def cmd_verify(args) -> int:
    top = _repo(args)
    checks = deploys.verify_lock(
        top,
        env=args.env,
        bundle_dir=Path(args.bundle) if args.bundle else None,
        reachable_from=args.reachable_from,
        require_signed=args.require_signed,
    )
    errors = [c for c in checks if c.level == "error"]
    warnings = [c for c in checks if c.level == "warning"]
    ok = not errors and not (args.strict and warnings)
    if args.github:
        for c in checks:
            where = f"{c.env}/{c.prompt}" if c.prompt != "-" else c.env
            level = "error" if c.level == "error" or args.strict else "warning"
            print(_github_annotation(level, f"promptpin verify: {where}", c.message))
        if ok and not checks:
            _say("verified: every pin re-derived from git matches the lock")
        return 0 if ok else 1
    if args.json:
        import json

        payload = {"ok": ok, "strict": args.strict, "checks": [c.to_mapping() for c in checks]}
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0 if ok else 1
    for c in checks:
        where = f"{c.env}/{c.prompt}" if c.prompt != "-" else c.env
        _say(f"[{c.level}] {_one(where)}: {_one(c.message)}")
    pins = deploys.read_lock(top) if (top / LOCK_FILENAME).exists() and not errors else {}
    n = sum(len(v) for e, v in pins.items() if args.env in (None, e))
    if ok:
        _say(f"verified {n} pin(s): every pin re-derived from git matches {LOCK_FILENAME}")
        if warnings:
            _say(f"({len(warnings)} warning(s) above)")
        return 0
    _say(f"\nverify FAILED ({len(errors)} error(s), {len(warnings)} warning(s))")
    return 1


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
        _say(
            f"⚠ warning: {rel} has uncommitted changes — this release will NOT include them.\n"
            "  Commit first, or the tag will point at the old version."
        )
    sha = gitutil.head_sha(top) if not args.sha else gitutil.resolve_ref(top, args.sha)[0]
    # The prompt file must exist at the tagged commit.
    found: str | None = None
    for ext in (".yaml", ".yml", ".md"):
        candidate = f"{cfg.prompts_dir}/{args.name}{ext}"
        try:
            gitutil.show_file(top, sha, candidate)
            found = candidate
            break
        except PromptpinError:
            continue
    if found is None:
        raise PromptpinError(
            f"prompt {args.name!r} has no file in {cfg.prompts_dir}/ at {sha[:7]} — "
            "commit the prompt file first"
        )
    if args.as_ and not gitutil.is_version_label(args.as_):
        raise PromptpinError(f"--as {args.as_!r}: version labels must look like v4 (or 4)")
    version = args.as_ or gitutil.next_version(top, args.name)
    tag = gitutil.tag_name(args.name, version)
    existing = gitutil.list_version_tags(top, args.name)
    if tag in existing:
        raise PromptpinError(f"{tag} already exists — pass --as vN for a new version")
    message = args.message or f"release {args.name} {version}"
    gitutil.create_tag(top, args.name, version, sha, message)
    _say(f"released {args.name}@{version} → {tag} ({sha[:7]})")
    if prompt.model:
        _say(f"  model={prompt.model} params={prompt.params or {}}")
    _say(f"deploy it: promptpin deploy {args.name}@{version} --env <env>")
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
    _say(f"pinned {_label(name, result['display_ref'])} → {sha7(result['sha'])} on {args.env}")
    if result["previous_sha"]:
        _say(f"  previous: {sha7(result['previous_sha'])}")
    else:
        _say("  previous: (none — first deploy)")
    if result["model"]:
        _say(f"  model={result['model']}")
    if result["mutable_ref"]:
        _say("  ⚠ MUTABLE REF: the ref can move — the pin is frozen at the SHA above.")
        _say("    Prefer tags/SHAs for production deploys.")
    if result["commit_sha"]:
        _say(f"  committed {LOCK_FILENAME} as {sha7(result['commit_sha'])}")
    else:
        _say(f"  (not committed — {LOCK_FILENAME} has uncommitted changes)")
    return 0


def sha7(sha: str) -> str:
    return sha[:7]


# -------------------------------------------------------------- rollback ---


def cmd_rollback(args) -> int:
    top = _repo(args)
    result = deploys.rollback(top, args.name, args.env, note=args.note or "")
    _say(f"rolled back {args.name} on {args.env} → {sha7(result['sha'])}")
    _say(f"  (was {sha7(result['rolled_back_from'])})")
    if result["commit_sha"]:
        _say(f"  committed {LOCK_FILENAME} as {sha7(result['commit_sha'])}")
    return 0


# ---------------------------------------------------------------- status ---


def cmd_status(args) -> int:
    top = _repo(args)
    pins = deploys.read_lock(top)
    if args.json:
        envs_json = [args.env] if args.env else sorted(pins)
        _print_json(
            {
                "envs": {
                    env: {
                        name: {
                            "ref": pin.ref,
                            "sha": pin.sha,
                            "model": pin.model,
                            "mutable_ref": bool(pin.mutable_ref),
                            "deployed_by": pin.deployed_by,
                            "deployed_at": pin.deployed_at,
                        }
                        for name, pin in sorted(pins.get(env, {}).items())
                    }
                    for env in envs_json
                },
                "issues": [safety.one_line(i) for i in deploys.status_issues(top, pins)],
            }
        )
        return 0
    if not pins:
        _say(f"nothing deployed yet — {LOCK_FILENAME} is empty or missing")
        return 0
    envs = [args.env] if args.env else sorted(pins)
    for env in envs:
        prompts = pins.get(env, {})
        if not prompts:
            continue
        _say(f"## {env}")
        for name in sorted(prompts):
            pin = prompts[name]
            flag = " ⚠MUTABLE" if pin.mutable_ref else ""
            _say(
                f"  {name:24} {_one(pin.ref):12} {sha7(pin.sha)}"
                f"  {_one(pin.model or '(no model)'):18} by {_one(pin.deployed_by)}"
                f" {_one(pin.deployed_at)}{flag}"
            )
    issues = deploys.status_issues(top, pins)
    if issues:
        _say("\nissues:")
        for issue in issues:
            _say(f"  ⚠ {issue}")
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
        _say("no deploys recorded in the local ledger")
        return 0
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        mut = " ⚠MUTABLE" if r["mutable_ref"] else ""
        prev = f" (was {r['previous_sha'][:7]})" if r["previous_sha"] else ""
        note = f" — {_one(r['note'])}" if r["note"] else ""
        _say(
            f"{ts}  {_one(r['env']):12} {r['prompt']:20} {_one(r['ref']):24}"
            f" → {r['resolved_sha'][:7]}{prev} by {_one(r['actor'])}{mut}{note}"
        )
    return 0


# ------------------------------------------------------------------ lock ---


def cmd_lock(args) -> int:
    """Export a minimal runtime manifest: env → prompt → {sha, model, params, template_sha256}."""

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
    fsutil.atomic_write_text(out, yamlsafe.dump(manifest))
    _say(f"wrote {out} ({len(prompts)} prompts for {args.env})")
    return 0


def cmd_export(args) -> int:
    top = _repo(args)
    result = deploys.export_bundle(top, args.env, Path(args.out))
    _say(f"exported {len(result['prompts'])} prompts for {args.env} → {result['out']}/")
    _say(f"bundle_digest: {result['bundle_digest']}")
    _say("bundle with your app; load with promptpin.PinnedPrompts(<dir>, expected_digest=...)")
    return 0


# ------------------------------------------------- review / attest ---


def cmd_review(args) -> int:
    from promptpin import review as reviewmod

    top = _repo(args)
    text, ok = reviewmod.review(top, args.base, args.head)
    print(safety.sanitize(text), end="")
    return 0 if ok else 1


def _read_key(env_name: str) -> bytes:
    import os

    value = os.environ.get(env_name)
    if not value:
        raise PromptpinError(f"environment variable {env_name} is not set (the signing key)")
    return value.encode("utf-8")


def cmd_attest(args) -> int:
    import json

    from promptpin import attest

    top = _repo(args)
    statement = attest.build_statement(top, args.env)
    envelope = attest.sign(statement, _read_key(args.key_env))
    fsutil.atomic_write_text(Path(args.out), json.dumps(envelope, indent=2) + "\n")
    _say(f"attested {len(statement['subject'])} prompt(s) for {args.env} → {args.out}")
    _say(f"bundle_digest: {statement['predicate']['bundle_digest']}")
    _say("HMAC-signed with a shared secret: anyone holding the key can also forge it")
    return 0


def cmd_attest_verify(args) -> int:
    from promptpin import attest

    top = _repo(args)
    statement = attest.read_verified(args.file, _read_key(args.key_env))
    problems = attest.check_against_repo(top, statement, args.env)
    env = statement["predicate"].get("env")
    checks = deploys.verify_lock(
        top, env=env, bundle_dir=Path(args.bundle) if args.bundle else None
    )
    problems += [c.message for c in checks if c.level == "error"]
    if args.bundle and not problems:
        from promptpin.client import load_bundle

        try:
            load_bundle(args.bundle, expected_digest=statement["predicate"]["bundle_digest"])
        except (ValueError, OSError) as exc:
            problems.append(f"bundle: {exc}")
    for p in problems:
        _say(f"[error] {_one(p)}")
    if problems:
        _say(f"\nattestation FAILED ({len(problems)} problem(s))")
        return 1
    _say(f"attestation OK: signature valid and {env!r} pins match git and the lock")
    return 0


# ------------------------------------------------------------------- log ---


def _parse_duration(s: str) -> float:
    import time as _time

    now = _time.time()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1] in units and s[:-1].isdigit():
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
        _say("logged")
        return 0
    if args.log_cmd == "stats":
        conn = statedb.connect(top)
        try:
            since = _parse_duration(args.since) if args.since else None
            stats = statedb.serving_stats(conn, prompt=args.prompt, env=args.env, since_ts=since)
        finally:
            conn.close()
        _say(f"serving events: {stats['total']}")
        for v in stats["versions"]:
            lat = f"{v['avg_latency_ms']:.1f}ms" if v["avg_latency_ms"] is not None else "n/a"
            _say(
                f"  {v['sha'][:7]}  {v['model'] or '(no model)':18}"
                f"  n={v['count']} ({v['share']:.0%})  avg_latency={lat}"
                f"  error_rate={v['error_rate']:.1%}"
            )
        return 0
    if args.log_cmd == "verify":
        from promptpin import servecheck

        checks = servecheck.verify_serving(
            top,
            args.env,
            Path(args.db) if args.db else None,
            grace_seconds=args.grace_seconds,
            since_ts=_parse_duration(args.since) if args.since else None,
        )
        errors = [c for c in checks if c.level == "error"]
        warnings = [c for c in checks if c.level == "warning"]
        for c in checks:
            where = f"{c.env}/{c.prompt}" if c.prompt != "-" else c.env
            _say(f"[{c.level}] {_one(where)}: {_one(c.message)}")
        if errors or (args.strict and warnings):
            _say(f"\nlog verify FAILED ({len(errors)} error(s), {len(warnings)} warning(s))")
            return 1
        _say(f"log verify OK: every logged request in {args.env} used the pinned prompt")
        return 0
    if args.log_cmd == "prune":
        conn = statedb.connect(top)
        try:
            cutoff = _parse_duration(args.older_than)
            n = statedb.prune_serving(conn, cutoff)
        finally:
            conn.close()
        _say(f"pruned {n} serving events older than {args.older_than}")
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
                number = float(v)
            except ValueError as exc:
                raise PromptpinError(f"bad --metric {item!r}: value must be numeric") from exc
            if number != number or number in (float("inf"), float("-inf")):
                raise PromptpinError(f"bad --metric {item!r}: NaN and infinity are not scores")
            metrics[k] = number
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
        _say(
            f"recorded {len(metrics)} metric(s) for {_label(args.prompt, display)}"
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
        _say(f"no evals recorded for {args.name!r}")
        return 0
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        note = f" — {_one(r['note'])}" if r["note"] else ""
        _say(
            f"{ts}  {_one(r['ref']):14} {r['resolved_sha'][:7]}"
            f"  {_one(r['metric'])}={r['value']:.4g}"
            f"  suite={_one(r['suite'])} dataset={_one(r['dataset_sha'][:7])}"
            f"  by {_one(r['recorded_by'])}{note}"
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
        _say(f"## {name} ({rel})")
        for entry in gitutil.file_history(top, rel, limit=10):
            _say(
                f"  {entry['date'][:10]} {entry['sha'][:7]} {_one(entry['author']):20}"
                f" {_one(entry['message'])}"
            )
    conn = statedb.connect(top)
    try:
        rows = statedb.deploy_history(conn, prompt=args.prompt, limit=10)
    finally:
        conn.close()
    if rows:
        _say("\n## recent deploys")
        for r in rows:
            ts = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime(
                "%Y-%m-%d"
            )
            _say(
                f"  {ts} {r['env']:12} {r['prompt']:20} {r['ref']:20}"
                f" → {r['resolved_sha'][:7]} by {_one(r['actor'])}"
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
    p.add_argument("--version", action="version", version=f"promptpin {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="scaffold promptpin.yaml, prompts/, .gitignore")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("list", help="list prompts with latest released version")
    s.add_argument("--json", action="store_true", help="machine-readable output")
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

    s = sub.add_parser(
        "validate", help="schema, variable, hidden-character and best-effort secret checks"
    )
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument(
        "--github", action="store_true", help="print GitHub Actions annotations (::error ...)"
    )
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser(
        "verify",
        help="CI gate: re-derive every pin from git and check it matches promptpin.lock",
    )
    s.add_argument("--env", help="only this env")
    s.add_argument("--bundle", help="also check an exported bundle dir matches the pins")
    s.add_argument(
        "--reachable-from",
        metavar="REF",
        help="every pinned commit must be an ancestor of REF (e.g. origin/main)",
    )
    s.add_argument(
        "--require-signed",
        action="store_true",
        help="every pinned commit and the newest lock-file commit need a good git signature",
    )
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument(
        "--github", action="store_true", help="print GitHub Actions annotations (::error ...)"
    )
    s.set_defaults(func=cmd_verify)

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
    s.add_argument("--json", action="store_true", help="machine-readable output")
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
    ls = log_sub.add_parser(
        "verify", help="fail if any logged request used a prompt other than the pinned one"
    )
    ls.add_argument("--env", default="production")
    ls.add_argument("--db", help="serving log to check (default: .promptpin/state.db)")
    ls.add_argument("--since", help="only requests newer than this, e.g. 24h")
    ls.add_argument(
        "--grace-seconds",
        type=float,
        default=300.0,
        help="ignore requests this long after a pin went live (rolling restarts)",
    )
    ls.add_argument("--strict", action="store_true", help="warnings fail too")
    ls = log_sub.add_parser("prune", help="delete serving events older than a duration")
    ls.add_argument("--older-than", required=True, help="e.g. 30d")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser(
        "review", help="plain-language Markdown summary of pin changes versus a base ref (for PRs)"
    )
    s.add_argument("--base", required=True, help="ref to compare against, e.g. origin/main")
    s.add_argument("--head", help="ref with the new lock (default: working tree)")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("attest", help="write an HMAC-signed in-toto attestation for an env's pins")
    s.add_argument("--env", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--key-env", default="PROMPTPIN_ATTEST_KEY", help="env var holding the key")
    s.set_defaults(func=cmd_attest)

    s = sub.add_parser("attest-verify", help="check an attestation against the key, lock and git")
    s.add_argument("file")
    s.add_argument("--env")
    s.add_argument("--bundle", help="also check this bundle dir matches the attested digest")
    s.add_argument("--key-env", default="PROMPTPIN_ATTEST_KEY")
    s.set_defaults(func=cmd_attest_verify)

    s = sub.add_parser(
        "js-verifier",
        help="print the path of (or copy out) the zero-dependency Node.js bundle verifier",
    )
    s.add_argument("--out", help="copy the verifier to this file instead of printing its path")
    s.set_defaults(func=cmd_js_verifier)

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


def _safe_streams() -> None:
    """Never crash on a console that cannot show a character (Windows cp1252, LC_ALL=C).

    Unencodable characters print as ``\\uXXXX`` escapes instead of raising
    ``UnicodeEncodeError``. Streams that cannot be reconfigured (already replaced) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def cmd_js_verifier(args) -> int:
    from importlib import resources

    src = resources.files("promptpin").joinpath("promptpin-verify.mjs")
    if not src.is_file():
        raise PromptpinError("the Node.js verifier is missing from this installation")
    if not args.out:
        with resources.as_file(src) as real:
            print(real)
        return 0
    out = Path(args.out)
    fsutil.atomic_write_bytes(out, src.read_bytes())
    _say(f"wrote {out} - it is a library, not a command. In Node 18+:")
    shown = str(out) if out.is_absolute() or str(out).startswith(".") else f"./{out}"
    _say(f"  import {{ loadBundle }} from {shown!r};")
    return 0


def _os_error_message(exc: OSError) -> str:
    where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
    reason = exc.strerror or exc.__class__.__name__
    hint = ""
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (13, 30):
        hint = " — check that you may write here (read-only folder or file in use?)"
    return f"{reason}{where}{hint}"


def main(argv: list[str] | None = None) -> int:
    _safe_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except PromptpinError as exc:
        print(f"promptpin: error: {safety.sanitize(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `promptpin diff ... | head` — the reader went away; exit quietly.
        import os

        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141
    except OSError as exc:
        # Disk full, permission denied, file in use: a sentence, not a traceback.
        print(f"promptpin: error: {safety.sanitize(_os_error_message(exc))}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort guard; set PROMPTPIN_DEBUG=1 for details
        import os

        if os.environ.get("PROMPTPIN_DEBUG"):
            raise
        print(
            f"promptpin: internal error ({exc.__class__.__name__}): {safety.sanitize(exc)}\n"
            "This is a bug in promptpin, not in your prompts. Re-run with PROMPTPIN_DEBUG=1 to "
            "see the full trace, and report it at https://github.com/calasdan-sketch/promptpin/issues",
            file=sys.stderr,
        )
        return 70


if __name__ == "__main__":
    sys.exit(main())
