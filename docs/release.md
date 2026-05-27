# Release runbook

This is the maintainer-only runbook for cutting a `tiktok-mcp` release to PyPI. Read the whole file before your first release. Subsequent releases follow sections 2 and 3.

Audience: the maintainer (you) plus any future co-maintainer with push access and PyPI environment access. If you're not one of those people, you don't need this file.

## 1. Versioning policy

`tiktok-mcp` follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

- MAJOR: breaking changes to the MCP tool surface, env-var contract, or stored token format.
- MINOR: new tools, new resources, new opt-in capabilities, additive config knobs.
- PATCH: bugfixes, dependency bumps that don't change behaviour, doc-only changes that affect the shipped artifact.

Releases are production-only and use final `vX.Y.Z` tags. Do not cut `-rc`, `alpha`, or `beta` tags for this project unless the release workflow is explicitly reworked to handle pre-releases.

The version source is `git`, not a file. [`hatch-vcs`](https://github.com/ofek/hatch-vcs) reads the most recent annotated tag and writes `src/tiktok_mcp/_version.py` at build time. That file is gitignored. Don't edit `__version__` by hand. Don't add a `version = "..."` line to `pyproject.toml`. Both will be overwritten at the next build, and the result will mismatch the tag.

Concretely: the only way to set the released version is `git tag vX.Y.Z`. The tag IS the version.

## 2. Pre-release checklist

Before tagging anything, walk this list top to bottom. Skipping a step costs more time than running it.

1. **F1-F4 reviewers APPROVE.** Every Final Verification Wave reviewer in the plan must have returned `APPROVE`. If any returned `REJECT`, land the fix and re-run that reviewer before tagging.
2. **CHANGELOG updated.** Either you've merged the `release-please` PR (preferred, see section 7), or you've manually edited `CHANGELOG.md` with the section for this version. The `Unreleased` block should be empty when you tag.
3. **Local smoke green.** All of the following must exit 0:
   ```sh
   uv run pytest -q
   uv run mypy src/
   uv run ruff check src/
   ```
4. **`main` is clean.** `git status` reports a clean working tree, `git rev-parse --abbrev-ref HEAD` reports `main`, and `git pull --ff-only` is a no-op.
5. **PyPI trusted publisher configured.** Confirm the production PyPI trusted publisher exists for project `tiktok-mcp`, owner `sigvardt`, repository `tiktok-mcp`, workflow `release.yml`, and environment `pypi`.

## 3. Production release steps

Once the checklist is clean:

1. Sanity sweep one more time:
   ```sh
   git status
   git branch --show-current
   uv run pytest -q
   ```
   Clean tree, on `main`, tests green.
2. Tag the release. The leading `v` is required so `release.yml` matches `tags: ['v*']`:
   ```sh
   git tag v0.1.0 -m "Release v0.1.0"
   git push origin v0.1.0
   ```
3. GitHub Actions picks up the tag, runs `release.yml`, builds wheel + sdist with `uv build`, and publishes to PyPI via [`pypa/gh-action-pypi-publish@release/v1`](https://github.com/pypa/gh-action-pypi-publish) using OIDC trusted publishing. No API token is read from anywhere; the workflow exchanges a GitHub-issued OIDC token for a short-lived PyPI upload credential. See the [PyPI trusted publishers docs](https://docs.pypi.org/trusted-publishers/) for the protocol.
4. Within five minutes of the workflow finishing, the artifact page at `pypi.org/project/tiktok-mcp/0.1.0/` should return 200. If it doesn't, see section 5 (Rollback) before debugging in place.
5. Smoke-test the published artifact on all three supported platforms. See [uv's docs](https://docs.astral.sh/uv/) for installing `uvx`.
   ```sh
   uvx tiktok-mcp@0.1.0 --version    # macOS
   uvx tiktok-mcp@0.1.0 --version    # Linux VM
   uvx tiktok-mcp@0.1.0 --version    # Windows VM
   ```
   All three must print `0.1.0` and exit 0. A smoke that succeeds locally on macOS but fails on Linux is still a failed release; cut a patch.
6. Announce the release: GitHub release notes (auto-created by release-please), README badge bump if needed, downstream notifications.

## 4. Hotfix flow

A hotfix is a patch that branches from a released tag, not from `main`. This matters when `main` has already moved on and you don't want to ship unrelated work in a patch.

1. Branch from the tag you're patching:
   ```sh
   git checkout -b hotfix/v0.1.1 v0.1.0
   ```
2. Land the fix as one or more commits on that branch. Keep the diff small.
3. Cherry-pick the same fix onto `main` so the next minor release inherits it. Resolve conflicts on `main`, not on the hotfix branch.
4. Update `CHANGELOG.md` with a `0.1.1` section. If release-please is wired for hotfixes, let it open the PR on `main`; otherwise hand-edit the changelog on the hotfix branch.
5. Tag from the hotfix branch:
   ```sh
   git tag v0.1.1 -m "Hotfix v0.1.1"
   git push origin v0.1.1
   ```
6. The same `release.yml` workflow runs. Verify the published artifact the same way as in section 3.

## 5. Rollback

PyPI is immutable. You cannot delete a release once it's been uploaded. You can yank it (mark it as "do not install by default"), but the artifact stays reachable for pinned installs.

The rollback strategy is forward-only:

1. Revert the offending commit(s) on `main`:
   ```sh
   git revert <bad-sha>
   ```
2. Cut a new patch version with the revert included. Follow section 4 (hotfix flow) end to end.
3. If the original release is dangerous (data loss, secret exfiltration, supply-chain compromise), yank it on PyPI via the project's web UI. Yanking is reversible and is the only legitimate "undo" PyPI offers.

Things you must not do:

- Don't tell users to run `pip install --force-reinstall`. It papers over upstream bugs with cache-thrashing; the new artifact you publish in step 2 resolves cleanly without it.
- Don't try to re-upload the same version with new contents. PyPI rejects duplicate filenames, and even if it accepted them, mirrors will already have cached the bad bytes.
- Don't delete the bad git tag. The tag is part of the audit trail. Tag the fix as the next patch instead.

## 6. PyPI pending-publisher bootstrap (one-time)

First-time setup for trusted publishing. Do this once, before the first ever `vX.Y.Z` tag. After it completes, every subsequent release uses the same configuration with no further setup. Wave 5 task T39 records the operator's exact form values; the steps below are the canonical sequence.

1. Sign in to PyPI as the project owner and open the publishing page at `pypi.org/manage/account/publishing/`. Pick "Add a new pending publisher".
2. Fill the form with the values pinned for this project:
   - PyPI project name: `tiktok-mcp`
   - Owner: `sigvardt`
   - Repository name: `tiktok-mcp`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

   Submit. PyPI now reserves the project name for that exact GitHub workflow + environment combination. Any push from a different repo, branch, or workflow will be rejected by the OIDC verifier.
3. In GitHub, go to repo Settings -> Environments -> New environment, name it `pypi`. Add protection rules: required reviewers (at least one maintainer), and a deployment-branches policy that matches tag patterns `v*` only. This stops a hijacked main branch from triggering a publish.
4. Confirm `release.yml` grants `id-token: write` at the **job level only**, never at the workflow level. The OIDC token must be scoped tightly so other jobs in the same workflow can't mint one for unrelated purposes.
The GitHub workflow itself never holds a PyPI API token. The `pypa/gh-action-pypi-publish` action exchanges a GitHub-issued OIDC token for a one-time upload credential at the moment of publish. There is no secret to rotate, no token to leak, and no shared credential between maintainers.

## 7. CHANGELOG conventions

`CHANGELOG.md` follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). The structure is:

```
## [Unreleased]
### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security

## [0.1.0] - YYYY-MM-DD
...
```

[release-please](https://github.com/googleapis/release-please) (Wave 5 task T40) owns `CHANGELOG.md` once it's wired up. It opens a "chore(main): release X.Y.Z" PR that updates the changelog and bumps the version reference; merging that PR is what tags the release. Don't hand-edit `CHANGELOG.md` between release-please PRs; your edits will be clobbered on the next run.

Use Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `perf:`) so release-please can categorize each commit. `feat:` bumps MINOR; `fix:` bumps PATCH; anything with `!` or a `BREAKING CHANGE:` footer bumps MAJOR. The "Compare" links at the bottom of the changelog are managed by release-please too.

If release-please isn't running yet (Wave 5 not landed), edit `CHANGELOG.md` by hand on the release branch and link every entry to its commit or PR.

---

For the Wave 5 release workflow itself (T38), the pending-publisher operator walkthrough (T39), and the release-please configuration (T40), see the corresponding tasks in `.omo/plans/tiktok-mcp.md`.
