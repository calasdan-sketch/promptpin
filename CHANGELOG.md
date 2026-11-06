# Changelog

## 0.2.0 — unreleased

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
