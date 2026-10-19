# promptpin

**Git-native prompt registry.** Your prompts live as files in your repo.
Versions are git commits, releases are namespaced tags, and deployments are
pins in a committed `promptpin.lock` file. Local-first: no server, no vendor
database, nothing on your request hot path.

```bash
pip install promptpin
promptpin init
# edit prompts/greeter.yaml, commit it
promptpin release greeter              # tags promptpin/greeter@v1
promptpin deploy greeter@v1 --env production
```

## Why not Langfuse / PromptLayer / Maxim?

They're good tools — but your prompts live in *their* database. Langfuse
self-hosted still means Postgres + ClickHouse + workers to operate, and
versions are created via their UI/API. (Humanloop's users learned the hard
way in September 2025: when the vendor shuts down, your version history goes
with it.)

promptpin's bet: **the registry is the git repo you already have.**
Prompt changes go through PR review. `git log -p promptpin.lock` *is* your
deployment history. `git blame prompts/classifier.yaml` *is* your audit
trail. Your app reads a pinned bundle file at startup — no fetch, no cache
TTL, no 2am "why is production serving the wrong prompt."

## Concepts

| Concept | Implementation |
|---|---|
| Prompt | One file in `prompts/`: `<name>.yaml` or `<name>.md` (frontmatter + Markdown) |
| Version | Git commit SHA (the truth); human name = tag `promptpin/<name>@v3` |
| Release | `promptpin release <name>` creates the next version tag |
| Deploy | `promptpin deploy <name>@v3 --env production` pins the resolved SHA into the committed `promptpin.lock` |
| Rollback | `promptpin rollback <name> --env production` re-pins the previous SHA |
| Serving | App loads a `promptpin export` bundle; logs which SHA served each request |

## Quickstart

```bash
promptpin init                                   # scaffold config + example prompt
# edit prompts/greeter.yaml, then:
git add -A && git commit -m "tune greeter"
promptpin release greeter                        # → promptpin/greeter@v1
promptpin deploy greeter@v1 --env staging        # pin v1 to staging
promptpin deploy greeter@v1 --env production --note "looks good in staging"
```

### The 9am question: "why did accuracy drop?"

```bash
promptpin diff summarizer v12 v13
```

Three panels: **template** (unified diff), **config** (model/params/variables
changes), and **recorded evals** — scores attached to each version, shown with
their suite and dataset provenance. The eval panel is labeled what it is:
*recorded measurements, not proof the prompt change caused them.*

```bash
# attach eval evidence (suite + dataset are required provenance)
promptpin eval record --prompt summarizer --ref v13 \
  --suite nightly --dataset-sha 9f2c... \
  --metric accuracy=0.91 --metric latency_p50_ms=180
```

### Runtime: serve pinned prompts, log what served

```bash
# at build time: export the pinned templates for an env
promptpin export --env production --out ./prompt-bundle
# bundle ./prompt-bundle with your app — no git needed at runtime
```

```python
from promptpin import load_bundle, render_template, log_serve

bundle = load_bundle("./prompt-bundle")          # hash-checked against the pin
p = bundle["classifier"]
text = render_template(p["template"], ticket_text=ticket)

response = llm.chat(model=p["model"], **p["params"], messages=[{"role": "user", "content": text}])

log_serve("./state.db", prompt="classifier", sha=p["sha"],
          model=p["model"], request_id=req.id, latency_ms=elapsed_ms)
```

```bash
promptpin log stats --prompt classifier --env production --since 24h
# which SHA served how many requests, avg latency, error rate per version
```

### Everyday commands

```bash
promptpin list                        # prompts + latest released version
promptpin versions classifier          # all released versions
promptpin show classifier@v2 --var ticket_text="login broken"
promptpin status                      # what's pinned where
promptpin history --env production    # deploy ledger
promptpin validate                    # schema + variable + secret checks (CI-friendly)
promptpin audit --prompt classifier   # git history + deploys: who changed what
promptpin lock --env production --out manifest.yaml   # minimal runtime manifest
```

## Prompt file format

`prompts/classifier.yaml`:

```yaml
name: classifier
description: Ticket classifier
model: gpt-4o-mini
params:
  temperature: 0.2
  max_tokens: 200
variables:
  - ticket_text
template: |
  Classify this ticket: {{ticket_text}}
  Reply with one of: bug, feature, question.
```

Or Markdown, for domain experts who don't want YAML blocks —
`prompts/summarizer.md`:

```markdown
---
name: summarizer
model: gpt-4o-mini
params:
  temperature: 0.5
variables:
  - text
---
Summarize in one sentence:

{{text}}
```

## Configuration (`promptpin.yaml`)

```yaml
prompts_dir: prompts
envs: [dev, staging, production]
log_sample_rate: 1.0        # 0..1 — sample the serving log before writing
auto_commit_deploys: true   # commit promptpin.lock on deploy (set false in CI)
```

Local state lives in `.promptpin/` (gitignored): `state.db` (deploy ledger,
evals, serving log) and the deploy mutex.

## Limitations (honest)

- **Eval scores are evidence, not proof.** Nothing stops someone recording a
  fake score — the defense is mandatory provenance (`--suite`,
  `--dataset-sha`, recorder identity), append-only history, and visible
  record counts, not prevention. `diff` never claims causation.
- **Serving log has no automatic rotation.** Use `log_sample_rate < 1` and
  `promptpin log prune --older-than 30d`. At 1.0 sampling and high traffic,
  `state.db` will grow — plan retention consciously.
- **The deploy ledger is local.** `history`/`rollback` see deploys made on
  *this* machine (`.promptpin/state.db`). The committed `promptpin.lock` is
  the cross-machine source of truth; rollback across machines means deploying
  the SHA you see in `status`.
- **Cross-machine same-env deploy races** resolve via git merge on
  `promptpin.lock`, not via locking. Different-env concurrent deploys merge
  cleanly; same-env conflicts surface as a normal merge conflict.
- **`latest` and branch deploys are allowed but flagged mutable.** The pin is
  frozen at the deploy-time SHA, but prefer tags/SHAs for production.
- **Secret scanning is a best-effort heuristic** (`validate` warns on
  obvious patterns). It will miss things. Use pre-commit hooks for real
  secret hygiene, and never commit secrets in prompts.
- **No prompt composition** (sub-prompt includes), no hosted UI, no
  automatic eval runs, no multi-repo sharing, no RBAC. The CLI and the lock
  file are the interface.
- If a branch and a tag share a name, git's own ref-ambiguity rules apply.

## Development

```bash
pip install -e ".[dev]"
pytest            # 105 tests
ruff check src tests
ruff format --check src tests
```
