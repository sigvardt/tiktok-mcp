<!-- markdownlint-disable MD013 MD034 -->

# PyPI pending-publisher bootstrap

One-time operator runbook for wiring `tiktok-mcp` to production PyPI via OIDC trusted publishing. Do this once, before the first production tag. Every subsequent release reuses the same configuration with no further setup.

This is an operator-only sequence because it requires logging into PyPI in a browser. The workflow never stores a PyPI API token.

## Why pending publishers, not API tokens

OIDC trusted publishing has no API token, no username, no password, and no shared secret. The trust boundary is a four-tuple: GitHub repository, workflow filename, GitHub Environment name, and the tag that triggered the workflow. PyPI verifies the GitHub-issued OIDC token against the pending-publisher record at upload time; nothing else can publish to the reserved project name.

That removes the rotation chore, removes the "who has the token" question, and removes the secret-leak blast radius. The cost is that the form on PyPI must be filled in before the first publish; PyPI calls this state "pending" until the first successful upload promotes it to "active".

## Step 1: register the PyPI pending publisher

1. Open `https://pypi.org/manage/account/publishing/` and sign in as the operator who will own the PyPI project.
2. Click "Add a new pending publisher".
3. Fill the form with these exact values:
   - PyPI Project Name: `tiktok-mcp`
   - Owner: `sigvardt`
   - Repository name: `tiktok-mcp`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
4. Save.

PyPI now reserves the name `tiktok-mcp` for that exact GitHub repo, workflow, and environment combination. Any tag push from a different repo, branch, workflow, or environment will be rejected by the OIDC verifier.

## Step 2: create the GitHub Environment

The workflow references the GitHub Environment named `pypi`. It must exist before the first production tag push, because the workflow's `environment.name:` reference fails closed if the environment is missing.

1. In GitHub, open the repo Settings, then Environments, then "New environment".
2. Name the environment `pypi`.
3. Optional but recommended: add required reviewers and a deployment-branches policy that restricts deploys to tags matching `v*`.

## Step 3: verify the binding

These checks are agent-runnable and prove the environment exists. The OIDC binding is proven by the first successful publish, when the pending publisher transitions to active.

1. Confirm the GitHub Environment exists:
   ```sh
   gh api /repos/sigvardt/tiktok-mcp/environments/pypi --jq '.name'
   ```
   Expected output:
   ```text
   pypi
   ```
2. Push the production tag (see `docs/release.md` § 3) and watch the `release.yml` workflow run. On success, the PyPI pending publisher transitions to active. Verify with:
   ```sh
   curl -fsSL "https://pypi.org/pypi/tiktok-mcp/<version>/json" | jq -e '.info.name == "tiktok-mcp"'
   ```

## Troubleshooting

The most common failure mode is a four-tuple mismatch between the pending-publisher form and the workflow file. If the workflow logs report `invalid-publisher`, `no project found`, or an `id_token` upload error:

- Reread the PyPI publishing page for the project and confirm the four fields: owner, repository, workflow filename, and environment name. Capitalization counts. Trailing slashes count.
- Confirm the GitHub Environment named in the form exists in the repo. A typo in either place breaks the binding.
- Confirm `release.yml` grants `id-token: write` at the job level only, never at the workflow level. The OIDC token must stay scoped to the publish job.

There is no token to rotate and no secret to leak. Fixing the four-tuple is the only repair path.

## References

- PyPI trusted publishers: `https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/`
- GitHub deployment environments: `https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment`
- This project's release runbook: `docs/release.md`
