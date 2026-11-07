# Changelog

## 0.3.0 — unreleased

### Added
- `PinnedPrompts` runtime verifier: startup + per-request checks, periodic full re-verify,
  `raise` / `last_good` failure policy, `Receipt` (commit, digests, rendered-prompt hash),
  `Receipt.annotate_span()` (OpenTelemetry, optional) and `Receipt.log()`.
- `promptpin log verify`: fails when the serving log holds requests for a non-pinned commit, drifted
  config digest or unpinned prompt. Serving log gains `digest` and `rendered_sha256` columns (auto-migrated).
- `verify --reachable-from REF` and `verify --require-signed` (offline provenance).
- `promptpin attest` / `attest-verify`: in-toto Statement v1 in a DSSE envelope, HMAC-SHA256 signed;
  `PinnedPrompts(attestation=..., attestation_key=...)`.
- `promptpin review --base REF`: plain-language, injection-safe Markdown for PR review.
- Whitespace-canonical classification (`promptpin.canon`); `diff` flags whitespace-only changes.
- `export` also writes `manifest.json`; `js/promptpin-verify.mjs` Node verifier; `verify --bundle`
  checks the two manifests agree.
- `benchmarks/run.py` with committed `RESULTS.md` (tamper matrix, false drift, runtime cost).

### Round 3 (hostile-input, availability and performance review)
Fixed
- `PinnedPrompts(on_failure="last_good")` crashed on a corrupt or deeply nested `manifest.yaml`
  (`yaml.YAMLError` / `RecursionError` escaped the failure policy). Any exception during a re-check now
  counts as a failed check. A bundle replaced on disk is retried once before it is called tampered.
- `log_serve` raised `database is locked` under concurrent writers and cost about 25 ms per call. It now
  keeps a per-thread connection (WAL, busy timeout, schema set up once, re-opens a deleted or replaced
  file) at about 0.1-0.2 ms. New `on_error="ignore"`.
- A slow full re-verify no longer blocks every `get()`: one thread re-reads, the rest serve the last
  verified bundle. Under `on_failure="raise"` every caller raises while the last check is failed.
- `validate` took over a minute on a 10 MB prompt and crashed with a traceback on deeply nested YAML.
  The hidden-character scan is linear (seconds to well under a second), nesting is a normal error.
- Lock file, manifests and exported templates are written atomically, as bytes (no CRLF on Windows),
  keeping the file mode. An unreadable lock file is a `DeployError`, not a traceback.
Added
- `validate` warns about look-alike words (Latin mixed with Cyrillic/Greek), malformed placeholders such as
  `{{user.name}}` (sent to the model literally), and errors on NaN/infinity in `params`.
- `served.render(..., scan_values="error")`, `PinnedPrompts.render(name, ...)`.
- `validate --github` / `verify --github` annotations; `verify --bundle` warns about unlisted
  `*.template.md` files.
- Benchmark: look-alike attack row, `validate --strict` column, log-append cost, concurrent-writer failures,
  scan speed, load average.
Behaviour changes
- Files over 2 MiB (`yamlsafe.MAX_FILE_BYTES`) are rejected everywhere promptpin parses them.
- The app-side serving log (`log_serve`) now uses WAL journaling, like the CLI's own database already did.
- `render(...)` has a keyword-only `scan_values` argument, so a template variable cannot be called that.

### Round 4 (the stranger's first hour: install, packaging, Windows review)
Fixed
- Printing `→` or `⚠` to an ASCII or Windows cp1252 console raised `UnicodeEncodeError` (a traceback from
  `diff`, `deploy`, `release`). Output now escapes what the console cannot show.
- `eval record --metric a=nan` (also `inf`, `1e999`) failed with a database error. It is now a plain refusal.
- Any OS error (read-only folder, disk full, file in use) and any unexpected error is now a one-line message
  (exit 1, or exit 70 for a promptpin bug; `PROMPTPIN_DEBUG=1` shows the trace), never a traceback.
- `init` wrote text-mode files (CRLF on Windows) and `attest --out` was not atomic; both now use the same
  atomic byte writer as the lock file. `init` no longer glues `.promptpin/` onto a `.gitignore` that lacks a final newline.
- Atomic replace is retried briefly on Windows when the target is in use (not run on Windows).
- `release` on a machine with no git identity says how to fix it.
- The published wheel had no LICENSE, no `py.typed`, no project URLs, and did not contain the JavaScript verifier
  that the README told people to use. Author is now "Calas Automations Inc.".
- A short commit SHA made only of digits (about 1 commit in 27 at 7 characters, e.g. `deploy greeter@3856106`) was read as a version
  number and failed with "does not resolve to a commit". A release tag still wins; otherwise 7+ digits are tried as a SHA.
- README said "20 tamper scenarios" while the table has 21.
Added
- `promptpin --version`; `promptpin` with no command prints help; `list --json` and `status --json`.
- `.github/workflows/ci.yml`: Linux, Windows, macOS x Python 3.9/3.13 (written, never run).
- `promptpin js-verifier [--out FILE]`: the Node verifier now ships in the wheel.
- `py.typed`, `mypy` clean (`pip install -e ".[dev]" && mypy`), classifiers, project URLs, `LICENSE`, `MANIFEST.in`.
Behaviour changes
- `deploy` and `eval record` print `greeter@v1`, not `greeter@promptpin/greeter@v1`.
- `js/promptpin-verify.mjs` moved to `src/promptpin/promptpin-verify.mjs` (use `promptpin js-verifier`).
- Build needs setuptools 77+ (SPDX `license = "MIT"`); `pip install` is unaffected.
- Recommended release number: 0.3.0 (PyPI has 0.1.1; 0.2.0 was never released). Pre-1.0, so the changes listed in the 0.2.0 and
  0.3.0 notes (digests cover model/params/format, bundles must be re-exported, files over 2 MiB rejected) ship together as a breaking minor.

### Compatibility
- Pins, lock files and bundles from 0.2.0 stay valid. Existing serving databases are migrated in place.

## 0.2.0 — unreleased

### Panel review fixes (second pass)
- **verify / bundles now cover model, params and format, not just the
  template.** Each lock pin and bundle entry carries a `digest` (SHA-256 of
  canonical JSON of template hash, model, params, format); bundles carry a
  `bundle_digest`. `verify` compares every field against git, `verify
  --bundle` compares the manifest against the lock, `export` refuses a lock
  that disagrees with git, and `load_bundle` rejects edited entries.
  `load_bundle(dir, expected_digest=...)` pins a specific build. Bundles
  exported before this change must be re-exported; pins deployed before it
  verify with a "no digest — redeploy" warning.
- Hidden-character scan: every Unicode format character (category Cf),
  Hangul/braille fillers, U+034F, runs of variation selectors ("emoji
  smuggling") and lone carriage returns are errors; a single U+FE0E/U+FE0F
  after a visible character is allowed. Params are scanned recursively
  (lists, nested mappings, keys), and variable names too.
- Git output and prompt files are read as bytes and decoded as strict UTF-8
  (CRLF normalized to LF), independent of the OS locale. A lone `\r` is no
  longer silently turned into a newline.
- YAML aliases are rejected everywhere promptpin reads YAML; the writer never
  emits them.
- A non-UTF-8 pinned file is reported as a `verify` check, not a traceback.
- `diff` warns when the *set* of invisible characters changes, not only the
  count.
- A non-string `env` in a bundle manifest is an error, not a crash.
- Tests build fake secrets at runtime and use neutral sample data.

### Added
- `promptpin verify`: CI gate that re-derives every pin in `promptpin.lock`
  from git (commit exists, prompt file exists at the SHA, name matches,
  `template_sha256` matches, env is configured) and fails closed on a missing,
  empty or malformed lock. `--bundle DIR` also checks an exported bundle is
  not stale; `--env`, `--strict`, `--json`.
- Hidden-character scan in `validate`: bidi controls, zero-width characters,
  Unicode tag characters and C0/C1 controls are errors; zero-width joiners are
  warnings. Scans template, description, model and string params.
- `diff` flags versions that add invisible characters and shows them as
  `\uXXXX` escapes.
- Chat prompts: `format: chat` with `system:` / `developer:` / `user:` /
  `assistant:` role lines; `render_messages()` and `render()` in the client.
  Roles are split before substitution, so variables cannot forge roles.
- `validate --strict` and `validate --json`.
- `.pre-commit-hooks.yaml` (`promptpin-validate`, `promptpin-verify`).

### Changed (behaviour)
- `validate` now **fails when no prompts are found** (previously printed
  "all prompts valid"). A gate that checks nothing must not pass.
- All CLI output escapes control, bidi, zero-width and tag characters, so
  untrusted prompt text, notes or commit messages cannot drive the terminal.
- `release --as` only accepts version labels (`v4` or `4`).
- `load_bundle` refuses manifests that are empty or whose `file` entries
  point anywhere but `<name>.template.md` in the bundle directory.
- `export` refuses to write a template whose hash differs from the pin.

### Fixed
- Branches whose names start with `v` (e.g. `v2-experiment`) were treated as
  version labels and could not be deployed.
- Refs starting with `-` are rejected instead of reaching git as options.
- `promptpin.lock` is validated on read: invalid prompt names (e.g. path
  traversal), non-mapping entries, merge-conflict markers and non-string
  fields give a clear error instead of a traceback.
- `promptpin.yaml`: `envs: production` (a string) no longer becomes a list of
  letters; absolute or `..` `prompts_dir` is rejected; Windows backslashes
  are normalized; bad YAML is a friendly error.
- `show --var key` (no `=`) and `log stats --since ""` no longer crash.
- Bundles are written byte-exact, so the hash check cannot break on
  newline translation.
- `promptpin diff ... | head` exits quietly instead of a BrokenPipe traceback.
- Plain `pytest` works from a fresh clone; build artefacts (`__pycache__`,
  `*.egg-info`) are no longer tracked.

## 0.1.1
- Initial public release.
