# Publish checklist for promptpin (for Dan; nothing here has been run)

**Version to release: 0.3.0.** PyPI currently has 0.1.1 only (0.2.0 was never released). Going from
0.1.1 to 0.3.0 is a breaking change for anyone who already uses 0.1.1: pins made with 0.1.1 must be
re-deployed and bundles re-exported (the CHANGELOG explains why). That is acceptable before 1.0, but say so
in the release notes. The version is already 0.3.0 in `pyproject.toml`, `promptpin/__init__.py` and `CHANGELOG.md`.

## Check first (about 10 minutes)

1. Read `git log --oneline` and `CHANGELOG.md` once. Make sure you agree with the "Behaviour changes".
2. In the README, delete the blockquote that starts "Install note" once 0.3.0 is on PyPI (it says PyPI only has 0.1.1).
3. Push the branch to GitHub first and look at the **ci** run. It tests Windows and macOS for the first time ever.
   If Windows fails, do not publish until you have read the failure. Also replace the `actions/...@v4` tags with
   commit SHAs if you want to be strict.
4. Decide whether the GitHub repo's default branch is `main`. The "Changelog" link uses `HEAD`, which works either way.

## Build and test locally

```bash
python -m venv /tmp/pp-release && . /tmp/pp-release/bin/activate
pip install build twine
python -m build            # makes dist/promptpin-0.3.0.tar.gz and .whl
twine check dist/*         # must say PASSED twice
unzip -l dist/*.whl | grep -E "LICENSE|py.typed|promptpin-verify.mjs"   # all three must show
python -m venv /tmp/pp-try && /tmp/pp-try/bin/pip install dist/*.whl && /tmp/pp-try/bin/promptpin --version
```

## Upload (this spends nothing, but it is permanent: a PyPI version number can never be reused)

```bash
twine upload --repository testpypi dist/*     # optional dress rehearsal, needs a TestPyPI account/token
twine upload dist/*                           # the real one; needs a PyPI token for this project
git tag v0.3.0 && git push origin v0.3.0
```

Safer long term: set up "trusted publishing" for this repository in the PyPI project settings so no token
has to be stored anywhere.

## After

`pip install promptpin==0.3.0` in a fresh folder, then run the README quick start once.
