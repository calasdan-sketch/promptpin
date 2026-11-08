# Publish checklist for promptpin (for Dan; nothing here has been run for you)

**Version to release: 0.3.0.** PyPI currently has 0.1.1 only (checked read-only on 2026-11-08; 0.2.0 was never
released). Going from 0.1.1 to 0.3.0 changes some behaviour, so say so in the release notes. The version is
already 0.3.0 in `pyproject.toml`, `promptpin/__init__.py` and `CHANGELOG.md`, and a test keeps them equal.
The author shown on PyPI will be "Calas Automations Inc." (set in `pyproject.toml`; no email is listed).

## What changes for someone already using 0.1.1 (copy into the release notes)

- Their `promptpin.lock` keeps working. `promptpin verify` prints one warning per pin
  ("pin has no digest - redeploy it"); running `promptpin deploy NAME@REF --env ENV` again clears it.
- Bundles made by `promptpin export` in 0.1.1 are refused with "re-export the bundle". They run
  `promptpin export` again and ship the new bundle.
- Their local `.promptpin/state.db` (deploy ledger, evals, serving log) keeps working.

## Check first (about 10 minutes)

1. Read `git log --oneline` and the top of `CHANGELOG.md` once. Make sure you agree with the "Behaviour changes".
2. In the README, delete the blockquote that starts "Install note" once 0.3.0 is on PyPI (it says PyPI only has 0.1.1).
3. Push the branch to GitHub first and look at the **ci** run. It tests Windows and macOS for the first time ever.
   If Windows fails, do not publish until you have read the failure. Also replace the `actions/...@v4` tags with
   commit SHAs if you want to be strict.
4. Decide whether the GitHub repo's default branch is `main`. The "Changelog" link uses `HEAD`, which works either way.
5. The GitHub repository named in the package links (`calasdan-sketch/promptpin`) must exist and be public before
   upload, or the links on the PyPI page will lead nowhere. If the repository is private, remove the `[project.urls]`
   block from `pyproject.toml` first.

## Build and test locally

```bash
python -m venv /tmp/pp-release && . /tmp/pp-release/bin/activate
pip install build twine
python -m build            # makes dist/promptpin-0.3.0.tar.gz and .whl
twine check dist/*         # must say PASSED twice
unzip -l dist/*.whl | grep -E "LICENSE|py.typed|promptpin-verify.mjs"   # all three must show
unzip -p dist/*.whl '*/METADATA' | grep -E "^(Name|Version|Author|License)"   # Author: Calas Automations Inc.
python -m venv /tmp/pp-try && /tmp/pp-try/bin/pip install dist/*.whl && /tmp/pp-try/bin/promptpin --version
```

## Upload (this spends nothing, but it is permanent: a PyPI version number can never be reused)

```bash
twine upload --repository testpypi dist/*     # optional dress rehearsal, needs a TestPyPI account/token
twine upload dist/*                           # the real one; you are signed in to PyPI yourself
git tag v0.3.0 && git push origin v0.3.0
```

Safer long term: set up "trusted publishing" for this repository in the PyPI project settings so no token
has to be stored anywhere.

## After

`pip install promptpin==0.3.0` in a fresh folder, then run the README quick start once.
