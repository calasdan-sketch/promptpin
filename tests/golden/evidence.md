# promptpin evidence: production

- Made: 2026-01-02T03:04:05Z by promptpin <version>
- Repository head: `92ffb03c578729f36a38b889e60b008efb5ba1ad`
- Verification against git: **PASSED** (0 error(s), 0 warning(s))
- Newest lock commit signature: N
- Deploys whose recorded person differs from the git author: 0

## Pinned now

| Prompt | Version | Commit | Signature | Deployed by (claimed) |
|---|---|---|---|---|
| greeter | v1 | `975b22d` | N | Golden |
| support | 424a47550851c7a5dd3f60debba28cb312419986 | `424a475` | N | Golden |

## Deploy history (from git)

- 2026-01-01T00:00:00+00:00 pin support -> `424a475` commit `ef20dee` by Golden, signature N
- 2026-01-01T00:00:00+00:00 pin greeter -> `975b22d` commit `424a475` by Golden, signature N

## What this does not prove

- deployed_by and deployed_at in the lock are typed by whoever ran deploy; only the commit author, date and signature come from git.
- A good commit signature shows who committed, not that the change was reviewed; use --reachable-from for the reviewed branch and CODEOWNERS for approval.
- The serving log is written by the application; it shows what the app recorded.
- report_sha256 detects later edits only if the hash is stored somewhere the editor cannot reach. hmac_sha256 is a shared-secret MAC: anyone with the key can also forge it.

report_sha256: `a7d44cd4e4935cbf74b4afd115518b9782c5064eef4ed1637535e858b76d0f85`
