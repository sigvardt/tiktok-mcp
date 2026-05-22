# S2 Results: PyPI OIDC Trusted-Publishing Pending-Publisher Bootstrap

## Exact 5-Step Bootstrap Sequence

1. Register the package name with a pending publisher on TestPyPI under the operator's TestPyPI account, pointing to the GitHub repo `<owner>/tiktok-mcp`, workflow file `release-spike.yml`, environment `pypi`.
2. Create the GitHub repository environment `pypi` with optional protection rules (required reviewers off for spike; can tighten later).
3. Create the throwaway `spikes/release-spike/` directory with a minimum `pyproject.toml` (`hatchling` build backend, version `0.0.0a0`, name = chosen PyPI name, single `src/tiktok_mcp/__init__.py`).
4. Create the throwaway workflow `.github/workflows/release-spike.yml` that triggers on tag `vSPIKE-*`, builds with `uv build`, publishes to TestPyPI via `pypa/gh-action-pypi-publish@release/v1` with `repository-url: https://test.pypi.org/legacy/`.
5. Push tag `vSPIKE-0.0.0a0`; verify workflow run succeeds; verify `pip index versions <name> -i https://test.pypi.org/simple/` returns `0.0.0a0`.

## Package-Name Decision

Run before replacing `REPLACE_WITH_CHOSEN_NAME` in `spikes/release-spike/pyproject.toml`:

```sh
curl -sf https://pypi.org/pypi/tiktok-mcp/json -o /dev/null && echo TAKEN || echo AVAILABLE
```

- Command outcome: `<TAKEN|AVAILABLE>`
- Chosen package name: `<tiktok-mcp|tiktok-complete-mcp>`
- Recorded decision: `PyPI name: <chosen-name> (<AVAILABLE|FALLBACK — tiktok-mcp TAKEN>)`

## Pending-Publisher Form Values

| field name | value used |
| --- | --- |
| project name | `<chosen-name>` |
| owner | `<github-owner-or-user>` |
| repo | `tiktok-mcp` |
| workflow filename | `release-spike.yml` |
| environment name | `pypi` |

## Workflow Run

- Run URL: `<https://github.com/<owner>/tiktok-mcp/actions/runs/<run-id>>`
- Conclusion: `<success|failure|cancelled>`
- Duration: `<duration>`

## TestPyPI Verification

- JSON metadata command: `curl https://test.pypi.org/pypi/<name>/0.0.0a0/json`
- JSON metadata outcome: `<status/body summary>`
- pip index command: `pip index versions <name> -i https://test.pypi.org/simple/`
- pip index outcome: `<includes 0.0.0a0|does not include 0.0.0a0>`
- uvx smoke command: `uvx --index-url https://test.pypi.org/simple/ <name>@0.0.0a0 --version`
- uvx smoke outcome: `<tiktok-mcp 0.0.0a0|failure output>`
- TestPyPI artifact URL: `<https://test.pypi.org/project/<name>/0.0.0a0/>`

## Issues Encountered

- `<none|issue summary and resolution>`

## DECISION: <PASS|PARTIAL|FAIL>
