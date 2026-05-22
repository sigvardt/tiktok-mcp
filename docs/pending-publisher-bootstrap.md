<!-- markdownlint-disable MD013 MD034 -->

# PyPI + TestPyPI pending-publisher bootstrap

One-time operator runbook for wiring `tiktok-mcp` to PyPI and TestPyPI via OIDC trusted publishing. Do this once, before the first ever rc tag. Every subsequent release reuses the same configuration with no further setup.

This is an operator-only sequence because steps 1 and 3 require logging into PyPI and TestPyPI in a browser. The first-time form submission is captured as a checklist entry in `.omo/evidence/operator-unblock-checklist.md`; this doc is the canonical walkthrough that checklist points to.

## Why pending publishers, not API tokens

OIDC trusted publishing has no API token, no username, no password, and no shared secret. The trust boundary is a four-tuple: GitHub repository, workflow filename, GitHub Environment name, and the tag that triggered the workflow. PyPI verifies the GitHub-issued OIDC token against the pending-publisher record at upload time; nothing else can publish to the reserved project name.

That removes the rotation chore, removes the "who has the token" question, and removes the secret-leak blast radius. The cost is that the form on PyPI must be filled in before the first publish; PyPI calls this state "pending" until the first successful upload promotes it to "active".

## Order matters

Always do TestPyPI first, then production. Filling the production form before validating an rc on TestPyPI risks burning the project name on a half-published artifact, and PyPI releases are immutable. The order below is non-negotiable:

1. TestPyPI form (this doc, step 1).
2. GitHub Environments `testpypi` and `pypi` (step 2).
3. First rc tag publishes to TestPyPI, smoke passes (covered in `docs/release.md` § 2).
4. Production PyPI form (step 3).
5. First production tag publishes to PyPI (covered in `docs/release.md` § 3, T42).

If step 3's rc smoke fails, fix the rc, bump to the next rc number, and re-smoke. Do not skip ahead to the production form.

## Step 1: register the TestPyPI pending publisher

Do this before any tag is pushed.

1. Open `https://test.pypi.org/manage/account/publishing/` and sign in as the operator who will own the TestPyPI project.
2. Click "Add a new pending publisher".
3. Fill the form with these exact values:
   - PyPI Project Name: `tiktok-mcp`
   - Owner: `<github-org-or-username>`
   - Repository name: `tiktok-mcp`
   - Workflow filename: `release.yml`
   - Environment name: `testpypi`
4. Save.

TestPyPI now reserves the name `tiktok-mcp` for that exact GitHub repo, workflow, and environment combination. Any tag push from a different repo, branch, workflow, or environment will be rejected by the OIDC verifier.

A screenshot of the filled form can land at `docs/images/pending-publisher-testpypi.png` if the operator captures one during bootstrap. It is not required; the steps above are sufficient on their own.

## Step 2: create the GitHub Environments

Both environments live in the same repository and are created the same way. They must exist before the first tag push, because the workflow's `environment.name:` reference fails closed if the environment is missing.

1. In GitHub, open the repo Settings, then Environments, then "New environment".
2. Name the first environment `testpypi`. Optionally add protection rules: required reviewers (at least one maintainer) and a deployment-branches policy that restricts deploys to tags matching `v*-rc.*`. Save.
3. Click "New environment" again. Name the second environment `pypi`. Optionally add stricter protection rules: required reviewers (at least one maintainer) and a deployment-branches policy that restricts deploys to tags matching `v*` (excluding rc tags). Save.

The asymmetric environment names (`testpypi` vs `pypi`) are deliberate. They are the contract that routes rc tags to TestPyPI and production tags to PyPI; the workflow file references them by name, and they must match the names you submit on each pending-publisher form exactly.

## Step 3: register the production PyPI pending publisher

Do this only after the first rc has shipped to TestPyPI and `uvx --index-url https://test.pypi.org/simple/ tiktok-mcp@<rc-version> --version` exits 0. The TestPyPI smoke is the gate; production registration before that gate risks claiming the production project name on a broken artifact.

1. Open `https://pypi.org/manage/account/publishing/` and sign in as the operator who will own the production PyPI project.
2. Click "Add a new pending publisher".
3. Fill the form with these exact values:
   - PyPI Project Name: `tiktok-mcp`
   - Owner: `<github-org-or-username>`
   - Repository name: `tiktok-mcp`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
4. Save.

Note the environment name asymmetry: TestPyPI's pending publisher points at `testpypi`, production's points at `pypi`. The repo, workflow, and project name are identical across both forms; only the environment name differs. That single field is what routes prod traffic away from the rc path.

A screenshot of the filled form can land at `docs/images/pending-publisher-pypi.png` if the operator captures one. Again, optional.

## Step 4: verify the bindings

These checks are agent-runnable and prove the environments exist. They do not prove the OIDC binding is active; that proof comes from the first successful publish (the "pending to active" transition fires on the first upload to each index).

1. Confirm both GitHub Environments exist:
   ```sh
   gh api /repos/<github-org-or-username>/tiktok-mcp/environments/testpypi
   gh api /repos/<github-org-or-username>/tiktok-mcp/environments/pypi
   ```
   Both calls must return HTTP 200 with a JSON body that includes the environment name. A 404 means step 2 was skipped for that environment.
2. Push the first rc tag (see `docs/release.md` § 2) and watch the `release.yml` workflow run. On success, the TestPyPI pending publisher transitions to active. Verify with:
   ```sh
   curl -fsSL "https://test.pypi.org/pypi/tiktok-mcp/<rc-version>/json" | jq -e '.info.name == "tiktok-mcp"'
   ```
3. After the production tag publishes (covered in `docs/release.md` § 3), the production pending publisher transitions to active. Verify with:
   ```sh
   curl -fsSL "https://pypi.org/pypi/tiktok-mcp/<version>/json" | jq -e '.info.name == "tiktok-mcp"'
   ```

If step 1 returns 200 for both environments and step 2 publishes cleanly, the rc path is wired. The production binding is independent: it only activates on the first production publish, which is gated separately in `docs/release.md`.

## Troubleshooting

The most common failure mode is a four-tuple mismatch between the pending-publisher form and the workflow file. If the workflow logs report "no project found" or the upload step fails with an `id_token` error:

- Reread the PyPI publishing page for the project and confirm the four fields (owner, repository, workflow filename, environment name) match the workflow exactly. Capitalization counts. Trailing slashes count.
- Confirm the GitHub Environment named in the form exists in the repo. A typo in either place breaks the binding.
- Confirm `release.yml` grants `id-token: write` at the job level only, never at the workflow level. The OIDC token must stay scoped to the publish job.

There is no token to rotate and no secret to leak. Fixing the four-tuple is the only repair path.

## References

- PyPI trusted publishers: `https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/`
- GitHub deployment environments: `https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment`
- This project's release runbook: `docs/release.md`
- Operator unblock checklist (one-time external steps): `.omo/evidence/operator-unblock-checklist.md`
