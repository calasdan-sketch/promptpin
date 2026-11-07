# Round 4: the stranger's first hour

Scope: what someone gets from `pip install promptpin` and the README, on a clean machine.
Rounds 1-3 covered the security model and hostile input; this round did not repeat them.

## How this was reviewed (honest)

No sub-agent tool was available in this run, so the eight specialist roles below were played one after
another by a single reviewer. They are separate checklists with separate commands, not independent minds;
an outside reviewer could still find things this one did not. Every finding lists the command that showed it.
Everything ran on Linux (the full suite on Python 3.11 and 3.13). Nothing ran on Windows, macOS or Python 3.9.

## Baseline (before round 4)

- `pytest`: 313 passed, 1 skipped (4 min 12 s, Python 3.13). `ruff check`: clean. `mypy`: 10 errors (not configured).
- Registry (read-only `pip index versions promptpin`, PyPI JSON): **0.1.1** is the only release (author "Calas Automations Inc.",
  uploaded 2026-10-22). The repo is 0.3.0; 0.2.0 was never released.
- Built sdist + wheel offline-style into a scratch directory; `twine check` passed, but the wheel had no LICENSE,
  no `py.typed`, no project URLs and no JavaScript verifier.

## Specialists and findings

| Specialist | Finding | Command that showed it | Fixed |
|---|---|---|---|
| First-time user (wheel in a fresh venv, folder name with a space) | `promptpin --version` does not exist | `promptpin --version` printed "arguments are required: cmd", exit 2 | yes |
| First-time user | README quick start omits `git init`; `init` outside git errors (clearly) | ran README top block in an empty folder | README fixed |
| First-time user | `release` on a machine with no git identity shows git's raw text only | `git init` with no `user.name`, then `promptpin release greeter` | hint added |
| First-time user | `deploy` prints `greeter@promptpin/greeter@v1` | `promptpin deploy greeter@v1 --env production` | yes |
| Release engineer | Wheel has no LICENSE although metadata says MIT | `unzip -l dist/*.whl` | LICENSE added, `license-files` set |
| Release engineer | No `py.typed`, classifiers, project URLs; author line "Calas Automations" (PyPI 0.1.1 says "Calas Automations Inc.") | `unzip -p ... METADATA` | yes |
| Release engineer | JS verifier lived in `js/`, outside the wheel; README pointed at it | `unzip -l`, README grep | moved into package, `promptpin js-verifier` |
| Release engineer | sdist lacks CHANGELOG, benchmarks | `tar tzf dist/*.tar.gz` | `MANIFEST.in` |
| Supply-chain reviewer | One dependency (PyYAML), no network calls, no `shell=True`, `yaml.safe_load` only, no pickle/eval | `grep -rn "pickle\|eval(\|shell=\|urlopen\|socket" src` (empty) and `pip list` in the clean venv | nothing to fix |
| Supply-chain reviewer | No personal paths or contiguous secret strings in repo or wheel | `grep -rIl "/home/\|/root/" ...`, secret-pattern grep | clean |
| Supply-chain reviewer | New CI workflow pins `actions/*` by tag, not SHA (cannot invent SHAs offline) | reading `.github/workflows/ci.yml` | noted; pin SHAs before relying on it |
| Docs/DX writer | README says "20 tamper scenarios", table has 21 | count of rows in `benchmarks/RESULTS.md` | yes |
| Docs/DX writer | Comparison "License" row said "unverified" for 3 tools | PyPI JSON for langfuse 5.2.0 (MIT), mlflow-skinny 3.18.0 (Apache-2.0), promptlayer 1.5.17 (Apache-2.0) | row updated, scoped to the Python packages |
| CI/CD integrator | Non-ASCII output crashes on ASCII/cp1252 consoles (the arrow in `diff`, `deploy`) | `PYTHONIOENCODING=ascii promptpin diff greeter v1 v1` -> `UnicodeEncodeError` | yes |
| CI/CD integrator | `list` and `status` have no `--json` | `promptpin status --json` -> unrecognized | added |
| CI/CD integrator | Concurrent deploys to two envs | two background `promptpin deploy` calls, then `verify` | OK (3 pins verified, no corruption) |
| Hostile-input fuzzer | `eval record --metric a=nan` -> database error traceback | `promptpin eval record ... --metric a=nan` | refused in plain words |
| Hostile-input fuzzer | ~30 odd inputs (bad refs, `../`, empty names, `/dev/full`, nonexistent paths, binary and empty prompt files, CRLF, BOM, 100 kB value) | shell loop flagging any "Traceback" or exit above 1 | only the NaN case; `/dev/full` now gives "No space left on device" |
| Performance engineer | CLI start-up about 0.1 s of CPU; `validate` on 300 prompts 1.0 s; `list` on 300 prompts 2.3 s (one git call per prompt) | `time promptpin ...` in a 300-prompt repo | not changed (idea 17) |
| Competitor's maintainer | Re-checked versions the comparison cites: langfuse 5.2.0, mlflow-skinny 3.18.0, promptlayer 1.5.17 are still current; Langfuse trace-link page still exists (HTTP 200) | PyPI JSON, `curl -I` | table still accurate; GitHub pages of PromptGit and Prompty could not be read here (unverified) |
| Python 3.11 run of the suite (Pass B) | `test_resolve_sha_is_immutable` failed once: a 7-digit-only short SHA (`3856106`) was read as version number 3856106. Real bug, about 1 commit in 27, not a flaky test | `pytest` on Python 3.11 | fixed, test added |
| Windows reviewer (static only) | `os.replace` on a file another process holds open raises PermissionError | reading `fsutil.py` | retry loop (Windows only) |
| Windows reviewer | `init` and `attest --out` wrote text mode (CRLF) | reading `cli.py` | now byte writes |
| Windows reviewer | No signal use; no `shell=True`; paths sent to git use `as_posix`; lock uses `msvcrt`; subprocess output decoded as UTF-8 | grep + reading | OK on paper |

## Scored idea list

Scores 1-5 (5 = best for value and differentiation; for effort and risk, 5 = most effort / most risk).

| # | Idea | User value | Differentiation | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | `--version` | 4 | 1 | 1 | 1 | DONE |
| 2 | Ship the JS verifier in the wheel, `js-verifier` command | 4 | 4 | 2 | 1 | DONE |
| 3 | LICENSE file in sdist and wheel | 5 | 1 | 1 | 1 | DONE |
| 4 | `py.typed` and a clean `mypy` run | 3 | 2 | 2 | 1 | DONE |
| 5 | Project URLs, classifiers, author "Calas Automations Inc." | 3 | 1 | 1 | 1 | DONE |
| 6 | Never crash on an unencodable console | 4 | 1 | 1 | 1 | DONE |
| 7 | OS errors as one sentence | 4 | 1 | 2 | 1 | DONE |
| 8 | Last-resort "internal error" exit 70 with `PROMPTPIN_DEBUG` | 3 | 1 | 1 | 1 | DONE |
| 9 | Reject NaN/infinity eval scores | 3 | 1 | 1 | 1 | DONE |
| 10 | Byte-exact, atomic `init` / `attest` writes | 3 | 1 | 1 | 1 | DONE |
| 11 | Git-identity hint on `release` | 3 | 1 | 1 | 1 | DONE |
| 12 | Windows replace-retry | 2 | 1 | 1 | 2 | DONE (unrun on Windows) |
| 13 | Short `greeter@v1` labels | 2 | 1 | 1 | 1 | DONE |
| 14 | `list --json`, `status --json` | 4 | 2 | 2 | 1 | DONE |
| 15 | Matrix CI (Linux/Windows/macOS, 3.9/3.13) | 5 | 1 | 2 | 1 | DONE (never run) |
| 16 | README: install note, exit codes, Windows limits, license row | 4 | 1 | 1 | 1 | DONE |
| 17 | Batch the per-prompt git calls in `list` / `status` | 3 | 1 | 3 | 2 | NOT-DONE |
| 18 | `promptpin doctor` (git present, identity set, repo state, version) | 3 | 2 | 2 | 1 | NOT-DONE |
| 19 | `--json` on `history`, `audit`, `show`, `diff` | 3 | 2 | 3 | 1 | NOT-DONE |
| 20 | Traffic split / A/B between two pins | 4 | 3 | 5 | 4 | NOT-DONE (earlier "not built") |
| 21 | Prompt includes / composition | 4 | 3 | 5 | 4 | NOT-DONE (earlier "not built") |
| 22 | Public-key / Sigstore signing instead of HMAC | 4 | 5 | 5 | 4 | NOT-DONE (earlier "not built") |
| 23 | Read-only-filesystem tests run as a non-root user | 2 | 1 | 2 | 1 | NOT-DONE (the sandbox user is root, which ignores file modes) |
| 24 | Pin GitHub Action versions by commit SHA | 3 | 1 | 1 | 1 | NOT-DONE (needs network to look the SHAs up) |
| 25 | PyPI trusted publishing instead of a token | 3 | 1 | 2 | 2 | NOT-DONE (Dan's account settings; in the checklist) |
| 26 | `NO_COLOR` / `--quiet` | 2 | 1 | 2 | 1 | NOT-DONE |

Competitor re-check (read-only, two closest): **Langfuse** (hosted/self-hosted server; prompt labels, trace links; its Python
SDK exposes no content hash on a prompt object in 5.2.0) and **MLflow Prompt Registry** (server, aliases; no hash on
`PromptVersion` in 3.18.0). Both statements are from the installed-package checks already cited in the README and were not
re-run this round beyond confirming the versions are still the latest. Neither does an offline "do the pins match source" gate,
which stays promptpin's difference. Their strengths we do not match: a UI, traffic splitting, non-engineer editing.

## Pass B (attacking the new code)

Re-ran the specialists against the new code, built the wheel again from the final tree, installed it in a new venv and ran
every README quick-start command, in a folder whose name has a space, with no global git identity.

| Check | Result |
|---|---|
| `twine check` on the rebuilt sdist and wheel | PASSED; wheel holds LICENSE, `py.typed`, `promptpin-verify.mjs`; sdist holds CHANGELOG, docs, benchmarks |
| Quick start: `init`, `release`, `deploy`, `verify`, `status --json`, `list --json`, `export`, `verify --bundle`, `PinnedPrompts` | all exit 0 with the output the README shows |
| `promptpin --version`, bare `promptpin` | `promptpin 0.3.0`; help and exit 2 |
| Exit 141 (README claims it) | `promptpin show ... (120 kB) | head -c 1` gave 141 |
| ASCII / cp1252 console | `diff` prints `\u2192`, no traceback |
| Defect found in Pass B | `js-verifier --out v.mjs` printed `import ... from 'v.mjs'`, which Node reads as a package name; now `'./v.mjs'` (test added) |
| README claim "tested on Linux only", "Windows unverified" | accurate |

## Numbers

| | Before | After |
|---|---|---|
| Tests (Python 3.13) | 313 passed, 1 skipped | 332 passed, 1 skipped |
| Tests (Python 3.11) | not run | 331 passed, 2 skipped after the fix (1 failed before it: the digit-SHA bug above) |
| `ruff check` / `ruff format --check` | clean | clean |
| `mypy src/promptpin` | 10 errors | 0 |
| Benchmark, tamper matrix | 21/21 caught | 21/21 caught (unchanged) |
| Benchmark, timings (noisy, machine load about 10) | `get()+render()` 23.7 / 21.9 / 27.4 us for 1/10/100 prompts; `check()` 1.27 / 15.5 / 197 ms; `log_serve` 0.16 ms | 23.2 / 24.4 / 19.8 us; 0.72 / 13.6 / 150 ms; 0.09 ms. No runtime code changed, so this is noise, not a speed-up. |

## What could not be tested

- Windows and macOS: not run. Python 3.9 and 3.10: not installed, syntax-checked only (`ast.parse(feature_version=(3, 9))`).
- Read-only filesystems and permission errors: the sandbox runs as root, so only a simulated `PermissionError` was tested.
- `git commit -S` / `--require-signed` end to end, GitHub Actions, pre-commit as a consumer: not run.
- The actual PyPI upload and install of 0.3.0: not done by design.
- Locale: ASCII and cp1252 output encodings were tested via `PYTHONIOENCODING`, not a real Windows console.
