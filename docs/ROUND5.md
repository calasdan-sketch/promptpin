# Round 5: prompt-ops, release, signing and audit review

Scope: the people who would run promptpin in a real release process (prompt-ops engineer, release manager),
the person who judges its tamper evidence (cryptographic-signing reviewer) and the person who has to hand
evidence to someone else (compliance auditor). Rounds 1-4 covered the security model, hostile input and
the first hour; this round did not repeat them.

## How this was reviewed (honest)

No sub-agent tool was used in this run, so the four specialist roles were played one after another by a single
reviewer, each with its own commands. They are separate checklists, not independent minds. Every finding names the
command that showed it. Everything ran on Linux. Windows and macOS were never run.

## Baseline (before round 5)

- `pytest` (Python 3.13): 331 passed, 2 skipped (3 min 23 s). `ruff check`, `ruff format --check`, `mypy`: clean.
- Benchmark: 21/21 tamper scenarios caught; load average about 10, so timings are noisy.
- Registry (read-only `pip index versions promptpin`): **0.1.1** is the only release.

## Upgrade-path test: the real 0.1.1 wheel to this tree

`pip download promptpin==0.1.1 --no-deps`, installed in its own venv, then: `init`, `release`, `deploy` (2 envs), `export`,
`log serve`, `eval record`, all with 0.1.1. Then the current tree on the same folder.

| What 0.1.1 made | Result with this tree | Evidence |
|---|---|---|
| `promptpin.lock` (no `format`, no `digest`) | loads; `verify` exits 0 with one warning per pin ("redeploy it"); a redeploy clears it | `promptpin verify` in the folder |
| `export` bundle | refused: "...re-export the bundle" (exit 1, clear text) | `promptpin verify --bundle`; `PinnedPrompts(...)` raises `PromptIntegrityError` |
| `.promptpin/state.db` (deploys, serving, evals) | works; `serving` gains two columns in place; evals still listed | `promptpin log stats`, `evals`, `diff` |
| Template hashes | unchanged (the 0.1.1 hash of the init template still matches the bytes) | `tests/test_round5.py::test_init_template_hash_is_unchanged_since_0_1_1` |

No migration command is needed. Real 0.1.1 output is committed as test fixtures (`tests/fixtures/v0_1_1/`).

## Specialists and findings

| Specialist | Finding | Command that showed it | Fixed |
|---|---|---|---|
| Prompt-ops / LLMOps engineer | `rollback` on a fresh clone or CI runner: "nothing to roll back to" although git holds the history (it read the local ledger) | `rm -rf .promptpin; promptpin rollback greeter --env staging` | yes: reads git history of `promptpin.lock`, ledger as fallback |
| Prompt-ops | `rollback` printed "(was X)" with the *target* commit instead of the replaced pin | `promptpin rollback ...` output | yes |
| Prompt-ops | `history` / `audit` empty on a fresh clone; no author or signature per deploy | `promptpin history` after `rm -rf .promptpin` | yes: git-derived, `--source ledger` keeps the old view, `--json` added |
| Prompt-ops | `auto_commit_deploys: false` in `promptpin.yaml` was ignored (README says to set it false in CI) | config `false`, then `promptpin deploy ...`: lock committed anyway | yes (`deploy`, `promote`) |
| Release manager | no way to ship "exactly what staging has" | by inspection; two manual `deploy` calls could resolve a moved tag differently | added `promote`; refuses a failing source pin or a moved tag |
| Release manager | 6 parallel `deploy` processes: one failed with git's `index.lock` error after changing the lock (commit ran outside the mutex) | 18 background deploys, 3 rounds; `err.txt` | yes: commit inside the mutex; 0 failures in 18; regression test fails without the fix |
| Release manager | `--limit 0` / `-3` gave empty or odd output | `promptpin history --limit -3` | yes: "--limit must be 1 or more" |
| Cryptographic-signing reviewer | `log verify` ignored every request in the first 300 s after a deploy, even for a commit never pinned | `log serve --sha deadbeef` right after a deploy, then `log verify` printed OK | yes: grace window still checks "was this commit ever pinned here" |
| Signing reviewer | Property test: one flipped character in the last base64 character of an envelope leaves the bytes (and MAC) valid, so one signed payload had several file forms | Hypothesis counter-example (`Draw 2: 253`) | yes: only canonical base64 accepted |
| Signing reviewer | Attestation key minimum was 16 bytes and `b"k"*32` was accepted | `attest.sign(..., b"k"*32)` | now 32 bytes and 8 distinct byte values; message shows how to make a key |
| Signing reviewer | An old attestation stays valid as long as the pins still match; no age | reading `attest.py` | `created_at` recorded; `attest-verify --max-age`; undated and future-dated fail closed |
| Signing reviewer | `keyid` is a SHA-256 prefix of the key, and the MAC itself already allows offline guessing of a weak key | reading `attest.py` | not changed: documented; the 32-byte/variety rule is the control. HMAC is still a shared secret, not public-key signing |
| Compliance auditor | `deployed_by` / `deployed_at` in the lock are typed by whoever ran `deploy` and are unsigned; `history` showed them as fact | reading `deploys.deploy` | history now shows the commit author/date/signature and flags where they differ |
| Compliance auditor | No single hand-over record | by inspection | `evidence` / `evidence-verify` (self-hashed, optional HMAC, says what it does not prove) |
| Compliance auditor | Markdown from untrusted text (names, notes) could add table cells | hostile `deployed_by` test | escaped; test |
| QA / test method (property-based) | `canonical_template` not idempotent for a lone carriage return (`"\r"` becomes `"\r\n"`, then `""`); `"\r \na"` likewise | Hypothesis counter-examples | yes: fixed point loop |
| QA / release | `benchmarks/run.py` crashed on Python 3.9 (`Path.write_text(newline=...)` is 3.10+), so `tests/test_benchmark.py` failed on 3.9; the CI matrix includes 3.9 | first full suite run on 3.9 (a 3.9 venv exists on this machine) | yes |
| QA | A git author name with a field separator or U+2028 could make a naive `git log` parse drop the commit (a way to hide a deploy) | `GIT_AUTHOR_NAME=$'evil\x1f '` test | parser uses a random separator and splits on `\n` only; skipped lines become a visible problem |

## Quality methods added

- **Property-based tests** (`tests/test_properties.py`, Hypothesis, skipped if not installed): canonical form vs an independent
  reference, idempotence, never hides bidi/tag characters, CRLF/trailing-space noise is cosmetic; `sanitize` output is terminal-safe
  and idempotent; chat variable values cannot forge roles; attestation round trip and "any one-character change is rejected";
  evidence seal detects any edited value. Found the two defects marked above.
- **Mutation-style sanity** (`python benchmarks/mutation_check.py`): 20 single-condition breakages (accept any MAC, skip template
  hash, skip bundle hash, invert the ancestor test, ...). First run: 18 of 20 killed; the two survivors showed a missing test
  (non-canonical base64, digest-only lock edit). Tests added. Final run: 20 of 20 killed (a later run also showed that no test edited
  only the lock's `template_sha256` field; test added).
- **Differential**: canonical form against a reference written as a line-by-line fixed point; JS verifier parity test unchanged.
- **Golden files** (`tests/golden/`): lock, bundle manifests (YAML and JSON), attestation statement and envelope, evidence JSON and
  Markdown, review Markdown, `status`/`list`/`history`/`verify` JSON. Commits use fixed dates and identities, so the files are
  identical on any machine. Refresh with `UPDATE_GOLDEN=1 pytest tests/test_golden.py`.

## Scored idea list

Scores 1-5 (5 = best for value and differentiation; for effort and risk, 5 = most effort / most risk).

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Git-derived deploy history (`history`, `audit`) | 5 | 4 | 3 | 2 | DONE |
| 2 | `rollback` that works on any clone | 5 | 2 | 2 | 2 | DONE |
| 3 | `promote` staging to production | 5 | 3 | 2 | 2 | DONE |
| 4 | `evidence` report + `evidence-verify` | 5 | 5 | 3 | 2 | DONE |
| 5 | Attestation freshness (`created_at`, `--max-age`) | 4 | 3 | 1 | 1 | DONE |
| 6 | Key strength rules, canonical base64 | 4 | 2 | 1 | 1 | DONE |
| 7 | Grace-window check for never-pinned commits | 4 | 4 | 2 | 1 | DONE |
| 8 | Commit inside the deploy mutex | 4 | 1 | 1 | 1 | DONE |
| 9 | Honour `auto_commit_deploys` | 4 | 1 | 1 | 1 | DONE |
| 10 | `--json` on history, audit, log stats, log verify | 3 | 2 | 2 | 1 | DONE |
| 11 | Property tests, golden files, mutation script, 0.1.1 fixtures | 4 | 2 | 3 | 1 | DONE |
| 12 | `__author__` and author-pinning tests | 2 | 1 | 1 | 1 | DONE |
| 13 | `--json` on `show`, `diff`, `versions`, `evals` | 3 | 2 | 3 | 1 | NOT-DONE |
| 14 | Batch the per-prompt git calls in `list` / `status` (round 4 idea 17) | 3 | 1 | 3 | 2 | NOT-DONE |
| 15 | `promptpin doctor` (git, identity, shallow clone, version) (round 4 idea 18) | 3 | 2 | 2 | 1 | NOT-DONE |
| 16 | Public-key signing (SSH/Sigstore) for attestations (round 4 idea 22) | 4 | 5 | 5 | 4 | NOT-DONE |
| 17 | Traffic split / A/B between two pins (idea 20) | 4 | 3 | 5 | 4 | NOT-DONE |
| 18 | Prompt includes / composition (idea 21) | 4 | 3 | 5 | 4 | NOT-DONE |
| 19 | Pin GitHub Action versions by commit SHA (idea 24) | 3 | 1 | 1 | 1 | NOT-DONE (needs network lookup) |
| 20 | PyPI trusted publishing (idea 25) | 3 | 1 | 2 | 2 | NOT-DONE (Dan's account settings) |
| 21 | `NO_COLOR` / `--quiet` (idea 26) | 2 | 1 | 2 | 1 | NOT-DONE |
| 22 | Read-only-filesystem tests as non-root (idea 23) | 2 | 1 | 2 | 1 | NOT-DONE (sandbox is root) |
| 23 | Release-notes generator between two tags (`review` for tags, not branches) | 3 | 2 | 2 | 1 | NOT-DONE |
| 24 | Env freeze / change window (refuse deploys on a frozen env) | 3 | 3 | 2 | 2 | NOT-DONE |
| 25 | Hash-chained serving log (tamper-evident rows) | 3 | 4 | 4 | 3 | NOT-DONE |
| 26 | Approval policy in lock (N distinct committers) checked offline | 4 | 4 | 4 | 3 | NOT-DONE |
| 27 | Tamper matrix rows for the new checks (stale attestation, grace window) | 3 | 2 | 2 | 1 | NOT-DONE (matrix unchanged: 21/21) |

## Competitor re-check (read-only, two closest)

`pip index versions`: langfuse 5.2.0, mlflow-skinny 3.18.0, promptlayer 1.5.17 are still the latest, as cited in the README.
The Langfuse prompt-management overview and the MLflow prompt-registry page both answered HTTP 200. The MLflow page text says prompt
versions are immutable and offers aliases, lineage, A/B testing and roll-backs; it mentions no content hash or tamper check. The Langfuse
overview page mentions labels and versions; no hash or signing. That is absence on one page each, not proof of absence elsewhere. Their
strengths we do not match: a UI, traffic splitting, non-engineer editing. Neither page was checked for audit-log features beyond that one page.

## Pass B (attacking the new code)

Re-read the diff as each specialist; ran every new README command from a built, non-editable install in a clean venv
(see the numbers below). Defects found in this pass and fixed:

| Check | Result |
|---|---|
| `evidence --sign` used a weaker key check than `attest` | now the same rules |
| `--limit 0` / negative | rejected in words |
| `deploy`/`promote` ignored config `auto_commit_deploys` | fixed (found by reading the new `promote` next to `deploy`) |
| Shallow clone: first event shown as "pin" although it is a later change | history prints a visible warning |
| Hostile author name / U+2028 in `git log` parse | test added, parser hardened |
| Odd locale (`LC_ALL=C`, `PYTHONIOENCODING=ascii`), a folder with a space, ~20 odd argument combinations on the new commands | no traceback, no exit 70 |

## Numbers

| | Before | After |
|---|---|---|
| Tests, Python 3.13 | 331 passed, 2 skipped | 385 passed, 2 skipped |
| Tests, Python 3.12 | not run | 385 passed, 2 skipped |
| Tests, Python 3.9 | not run (round 4: syntax-checked only) | 385 passed, 2 skipped (first real run; it found the benchmark crash above) |
| `ruff check` / `ruff format --check` / `mypy` | clean | clean |
| Mutation check | did not exist | 20 of 20 killed |
| Benchmark tamper matrix | 21/21 | 21/21 (unchanged; new checks are not rows in it, idea 27) |
| Benchmark timings | `get()+render()` 19.5 / 21.3 / 8.5 us (1/10/100 prompts), `check()` 0.73 / 17.3 / 79 ms at load about 10 | 6.3 / 5.8 / 9.2 us, `check()` 0.64 / 4.5 / 49 ms at load 1.4. No runtime code changed: the difference is machine load, not a speed-up. |
| Parallel deploys (6 processes x 3 rounds) | 1 of 6 failed in the first round | 0 of 18 failed |

Built from this tree (`python -m build`, `twine check` PASSED twice); wheel METADATA says `Author: Calas Automations Inc.`, `Version: 0.3.0`,
`License-Expression: MIT`; it contains LICENSE, `py.typed`, `promptpin-verify.mjs`, `history.py`, `evidence.py`; the sdist contains the tests,
fixtures and golden files. Installed non-editable in a clean venv and run from a folder with a space in its name: quick start,
`promote`, `rollback` without ledger, `history`, `evidence --sign` and `evidence-verify`, `attest` and `attest-verify --max-age`,
`export`, `verify --bundle`, `status --json`, `list --json`, `js-verifier`, `log stats --json` all behaved as the README says.

## What could not be tested

- Windows and macOS: never run (CI file still unrun). `git cat-file --batch` and `sha:path` specs are POSIX-style on every platform by design.
- Real GPG/SSH-signed commits end to end: signature codes are read from git but only the "not signed" path ran here.
- GitHub Actions, pre-commit as a consumer, the real PyPI upload: not run by design.
- Large histories with signing configured: `%G?` is skipped for rollback and `log verify`, but `history`/`evidence` ask git to check each
  commit's signature (measured: 150 commits, 2.5 s on a loaded machine without signing configured).
- Read-only filesystems: sandbox is root.
