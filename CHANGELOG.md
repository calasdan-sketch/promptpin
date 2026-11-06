# Changelog

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
