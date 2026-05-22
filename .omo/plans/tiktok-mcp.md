# TikTok MCP (handrolled, multi-account, uvx-distributed)

## TL;DR

> **Quick Summary**: Build a Python FastMCP stdio server (`tiktok-mcp`) that wraps four TikTok API surfaces — Display API, Marketing API, Business Accounts/Organic (comments), and Content Posting API — exposing full read + write functionality to Claude Desktop with multi-account OAuth, OS-keychain token storage, manual-paste auth (no localhost callback), env-var-gated writes, full TDD with vcrpy, and PyPI/uvx distribution via OIDC trusted publishing.
>
> **Deliverables**:
> - Python package `tiktok-mcp` (fallback name: `tiktok-complete-mcp`) on PyPI, runnable via `uvx tiktok-mcp`
> - 60-90 MCP tools across 4 API surfaces (reads + writes + setup tools) all with correct `readOnlyHint` / `destructiveHint` annotations
> - Multi-account OAuth subsystem (manual paste flow, no localhost) supporting unlimited personal/brand/advertiser accounts
> - OS-keychain token storage (`keyring`) with encrypted-file fallback (`cryptography.fernet`), namespaced by sandbox/production
> - Runtime safety env vars: `TIKTOK_MCP_ALLOW_WRITES` (comma-separated per-API gate, blocks production writes when unset) + `TIKTOK_MCP_ALLOW_LIVE_WRITES` (test-time live HTTP gate)
> - Comprehensive `BusinessApiResponse` envelope decoder; per-account `asyncio.Lock` on token refresh path; reactive rate-limit handling with exp backoff
> - Full pytest suite using vcrpy fixtures; CI matrix (3 OS × 3 Python versions); GitHub Actions release workflow with OIDC trusted publishing
> - README with `claude_desktop_config.json` example, account-setup walkthrough, security caveats
> - Documentation: `docs/api-surface-inventory.md`, `docs/auth-architecture.md`, `docs/security-model.md`, `docs/release.md`
>
> **Estimated Effort**: XL (~50-65 tasks across 7 waves)
> **Parallel Execution**: YES — 7 waves (3 gating spikes + 5 parallel implementation waves + final review wave + terminal release wave)
> **Critical Path**: S1 (redirect fidelity spike) → Wave 1 foundation → Wave 2 read tools (max parallel across 4 APIs) → Wave 3 write tools (gated) → Wave 4 polish → Wave 5 release pipeline (T37-T41) → Final Wave reviewers (F1-F4 APPROVE) → Wave 6 terminal release (T42 ships the tag)

---

## Decisions of Record (READ FIRST — supersedes anything else)

> **Plan-wide canonical decisions.** Where this section and any subsequent task disagree, this section wins. Where the original user request and a later interview decision disagree, the later decision wins. Interview decisions captured in `.omo/drafts/tiktok-mcp.md` are folded in below; that draft is deleted post-plan.

### 1. Four API surfaces in v0.1

1. **Display API** (developers.tiktok.com) — user info, video list/query, post performance metrics. OAuth via TikTok Login Kit.
2. **Marketing API** (business-api.tiktok.com) — Campaign / AdGroup / Ad CRUD + status (pause/resume), synchronous + asynchronous Integrated Reports, Business Center info (list BCs, list advertisers under BC), audience uploads (file-based custom audiences), creative uploads.
3. **Business API — Accounts/Organic** (business-api.tiktok.com) — list comments on own videos, list replies, post reply, pin/unpin top comment, hide/unhide, delete own reply.
4. **Content Posting API** (developers.tiktok.com) — video upload (FILE_UPLOAD chunked + PULL_FROM_URL), photo upload, draft inbox (default), status polling, Direct Post (opt-in `publish_immediately=True` only).

### 2. Explicit v0.1 OUT (deferred to v0.2)

Marketing API: Catalog Manager / DPA, Audience Segments / Lookalike, Reservation buying (auction only in v0.1), Pixel / Events API. Business Organic: comment search. Content Posting: slideshow / interactive features. Across the board: scraping (no-auth) endpoints, Research API (academic), non-Python clients, web UI, long-term analytics storage, telemetry.

### 3. Authentication is MANUAL PASTE (no localhost, no infrastructure)

TikTok developer consoles do NOT accept `localhost` or IP redirect URIs — only TLD URLs. The flow:

1. User asks Claude to add an account → Claude calls `add_account(api_type=, alias=)` MCP tool.
2. MCP generates auth URL with `state` (random, 10min TTL) + PKCE verifier (where supported) → returns URL + suggested alias.
3. User opens URL in browser, authenticates with TikTok.
4. TikTok redirects to the registered TLD (e.g. `https://oauth.example.com?code=XYZ&state=ABC`).
5. User copies the FULL redirect URL (or just `code` + `state`) from their browser address bar and pastes back into Claude.
6. Claude calls `complete_account_login(redirect_url=, alias_override=)`.
7. MCP parses URL → validates state → validates host → exchanges code for tokens → atomically writes to keychain → confirms.

**No localhost listener anywhere. No hosted relay. No infrastructure.**

### 4. Write/destructive tool safety (CRITICAL — non-negotiable)

Every write/delete/mutation tool:
- Annotated `destructiveHint: true` in its MCP tool annotation (Claude Desktop gates per-call).
- Decorated `@require_writes_enabled("<api_name>")` — checks env at EVERY invocation (toggling works mid-session, not just startup).

**Runtime env var `TIKTOK_MCP_ALLOW_WRITES`** — hard runtime gate, structured-error response when blocked:

| Value | Behaviour |
|---|---|
| (unset) / `""` / `"0"` / `"false"` / `"False"` / `"no"` | ALL writes blocked |
| `"1"` / `"true"` / `"True"` / `"yes"` / `"all"` | ALL writes enabled (legacy `1` = `all`) |
| `"marketing"` | Only Marketing API writes |
| `"comments"` | Only Business Organic comment moderation writes |
| `"posting"` | Only Content Posting writes |
| `"display"` | Only Display API writes (if any exist) |
| Comma-separated (e.g. `"marketing,comments"`) | Multiple specific surfaces |

Blocked error response shape:
```json
{
  "error": "writes_disabled",
  "message": "Write/delete tools for '<api>' are disabled. Set TIKTOK_MCP_ALLOW_WRITES=all (or include '<api>') to enable.",
  "tool": "<tool_name>",
  "api": "<api_name>",
  "would_have_done": "<short human-readable description>"
}
```

**Test env var `TIKTOK_MCP_ALLOW_LIVE_WRITES`** — separate test-time-only gate:

| Value | Behaviour |
|---|---|
| (unset) / `"0"` / `"false"` | All write tests run vcrpy-replay only; tests tagged `@pytest.mark.live_write` are auto-skipped |
| `"1"` / `"true"` | Live write tests run (manual local opt-in only) |

CI never sets `TIKTOK_MCP_ALLOW_LIVE_WRITES`. CI runs read live tests + all write tests in replay mode.

Read tools: no gate; live integration tests in CI (rate-limit aware).

### 5. Token storage

- **Primary**: OS keychain via `keyring` lib (macOS Keychain / Windows Credential Manager / Linux Secret Service).
- **Auto-fallback** on `keyring.errors.NoKeyringError`: AES-encrypted JSON file via `cryptography.fernet`, under `platformdirs.user_data_dir("tiktok-mcp")`, fernet key stored in keychain if possible.
- **Plain file**: NEVER acceptable.
- **Sandbox namespacing**: keychain key prefix `tiktok-mcp::<api>::sandbox::<alias>` vs `tiktok-mcp::<api>::production::<alias>`; accounts tagged `sandbox=true`; cross-mode access rejected.
- **Atomic refresh**: write new RT BEFORE discarding old; per-account `asyncio.Lock` on refresh path.

### 6. Concurrency

- Per-account `asyncio.Lock` on token-refresh path.
- In-memory token cache + keychain write under same lock; atomic swap.
- Parallel reads on same account: OK.
- Parallel writes on same account: TikTok handles; MCP does not serialize (documented as unspecified ordering).
- Parallel calls across accounts: full parallel; each account has own lock.

### 7. Rate limiting (v0.1)

Reactive only: respect `Retry-After` header on 429; exponential backoff with jitter; max 3 retries; then structured `rate_limited` error to caller. `get_rate_limit_status` MCP tool exposes posture.

### 8. Error envelopes

Single `BusinessApiResponse` decoder used by ALL Business API / Marketing API / Comments API calls. Routes `HTTP 200 + code != 0` to typed `BusinessApiError(code, message, request_id, context)`. Display API + Content Posting API use a separate `DisplayApiResponse` decoder (RESTful errors via HTTP status). httpx exceptions are wrapped to strip response bodies before bubbling up (secret/PII leak prevention).

### 9. Pagination

Native passthrough per API. NO unified abstraction. Cursor for Display, page for Marketing/Comments, polling for Content Posting.

### 10. Currency + timezone

Marketing reporting tools return explicit `currency_code` + `timezone` per row. NO cross-currency aggregation in v0.1. NO timezone normalization.

### 11. OAuth state

In-memory `dict[state_token, OAuthInProgress(api_type, pkce_verifier, suggested_alias, expires_at)]`. TTL 10 min. Single-use. MCP restart loses in-flight states; user told to restart flow.

### 12. Secret redaction (mandatory)

`SecretRedactor` `logging.Filter` on root logger. Pattern list seeded with `access_token=`, `refresh_token=`, `code=`, `client_secret=`, `Authorization:`. Runtime token set: every token read from keychain added to redaction set; filter replaces with `<REDACTED:token_name>`. httpx exception wrapper strips response body. Unit test asserts no token in caplog or exception strings.

### 13. PII / data retention

Comment text NEVER cached or persisted to disk. vcrpy cassettes for comment tests have comment-body fields scrubbed before commit. `--log-level=DEBUG` does NOT dump comment bodies; `TIKTOK_MCP_LOG_COMMENT_BODIES=1` is explicit opt-in.

### 14. Account model

`add_account` returns suggested alias `<country>-<type>-<short_id>`. User can override via `alias_override` at `complete_account_login`. Duplicate alias rejected with suggested next-available (`-1`, `-2`, ...). `rename_account` for later renames. `remove_account` is **two-step with confirmation_token** (60s TTL).

### 15. Packaging + distribution

- Build backend: `hatchling`
- Version source: `hatch-vcs` (git tags `v0.1.0`)
- Trigger: `on: push: tags: ['v*']`
- Publishing: GitHub Actions OIDC trusted publishing (`pypa/gh-action-pypi-publish@release/v1`), no API tokens
- Python: 3.11+
- License: MIT
- Entry point: `[project.scripts]` → `tiktok-mcp = "tiktok_mcp.server:main"`
- PyPI name: `tiktok-mcp` if available, else `tiktok-complete-mcp`

### 16. Test strategy

`pytest` + `pytest-asyncio` + `vcrpy`. Read tools TDD + live CI integration. Write tools TDD with vcrpy-replay-only in CI. Live writes behind `TIKTOK_MCP_ALLOW_LIVE_WRITES=1` for manual runs. Agent-Executed QA Scenarios mandatory on every task.

### 17. Setup ergonomics

MCP-tools-only (no separate CLI). Setup driven by Claude calling tools. PyPI entry point only launches the server.

### 18. Logging

stderr only. Default INFO. `TIKTOK_MCP_LOG_LEVEL`, `TIKTOK_MCP_LOG_FILE`, `TIKTOK_MCP_LOG_FORMAT=json` env vars. `SecretRedactor` always on, cannot be disabled.

---


### 19. Account-management gate (separate from writes)

`TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` — orthogonal env var that gates account add/remove/rename + app-credential management tools. Distinct from `TIKTOK_MCP_ALLOW_WRITES` because the user-permission profile is different:
- **`TIKTOK_MCP_ALLOW_WRITES`** = "can Claude mutate TikTok-side state (pause an ad, post a comment, upload a video)?"
- **`TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES`** = "can Claude mutate the local MCP's account inventory (add account, remove account, rotate app credentials)?"

A user onboarding to the MCP wants ACCOUNT_CHANGES enabled (so Claude can guide them through `add_account`) but might NOT want general ALLOW_WRITES enabled yet. Conversely, a advanced user with all accounts already added wants ALLOW_WRITES but might lock down ACCOUNT_CHANGES.

Both gates accept the same truthy/falsy syntax as `TIKTOK_MCP_ALLOW_WRITES` (unset/empty/0/false → blocked; 1/true/yes → enabled). No per-API granularity for ACCOUNT_CHANGES (it's binary). Two-step `remove_account` confirmation (60s TTL) applies even when this gate is enabled.

The Decisions of Record §4 mandate that destructive tools use `@require_writes_enabled` AND `destructiveHint`. Account-change tools satisfy `destructiveHint` (Claude Desktop sees them as destructive) but are decorated with `@require_account_changes_enabled` instead of `@require_writes_enabled`. This is an explicit, documented exception to the "every destructive uses @require_writes_enabled" rule, justified by the user-permission orthogonality above.

## Context

### Original Request
User wants to handroll a complete Python TikTok MCP supporting full featureset for Display + Marketing + Business comments APIs. Uses FastMCP as base. stdio transport targeting Claude Desktop. Distributed via PyPI `uvx`. Multi-account support across brand/advertising/personal accounts. MCP handles all OAuth token minting.

### Interview Summary (key points)
- **Scope expanded** mid-interview: from 3 APIs to 4 (Content Posting added) and from read-only to full read+write with destructive annotations.
- **Auth flow constraint discovered**: TikTok requires TLD redirect URIs (no localhost), forcing manual-paste flow.
- **Safety gating added**: env-var gate on all write tools (`TIKTOK_MCP_ALLOW_WRITES`) plus separate test-time gate (`TIKTOK_MCP_ALLOW_LIVE_WRITES`).
- **App registrations**: user provided Display API (production + sandbox) + Business API ("TikTok MCP" prod + sandbox) credentials; runtime loading via MCP `set_app_credentials` tool into keychain.

### Research Findings
- Display API: 3 endpoints (user info, video list, video query); Login Kit OAuth; PKCE for desktop, optional for web; access token 24h TTL, refresh token 365d with rotation; 600 req/min rate limit; no comment endpoints on Display.
- Marketing API: `/portal/auth` for OAuth; integrated reports endpoint with report_type/data_level/dimensions/metrics; HTTP 200 + non-zero `code` for business errors (critical for envelope decoder).
- Content Posting: separate from Display, shares Login Kit auth; chunked upload semantics.
- FastMCP: recommend official `mcp[cli]` SDK (canonical FastMCP class) for new Claude Desktop stdio servers; standalone `fastmcp` v3 by Prefect adds features but isn't necessary.
- TikTok SDKs: `tiktok-business-api-sdk-official` (sync only, generated from Swagger), `python-tiktok` by sns-sdks (Display + Business, requests + dataclasses). **Verdict**: handroll with `httpx` + `pydantic` v2 for full async + types + control.
- PyPI: trusted publishing via OIDC, pending-publisher flow for first publish, `hatchling` + `hatch-vcs` for tag-driven version.

### Metis Review
Folded into Decisions of Record (sections 6-14, 16). Key Metis additions: concurrency model, rate limit strategy, BusinessApiResponse decoder, refresh-token rotation atomicity, OAuth state TTL, SecretRedactor middleware, comment-text PII protection, two-step `remove_account`, per-API write granularity, three Wave-1 gating spikes.

---

## Work Objectives

### Core Objective
Ship a production-ready, multi-account, OAuth-driven Python MCP server that exposes the full feature set of four TikTok API surfaces (Display, Marketing, Business Organic comments, Content Posting) to Claude Desktop with strict write-tool safety gating, comprehensive vcrpy-driven TDD coverage, and a fully automated PyPI release pipeline.

### Concrete Deliverables
- PyPI package (~v0.1.0) installable via `uvx tiktok-mcp`
- 60-90 MCP tools (final count after API-surface inventory)
- `docs/api-surface-inventory.md` (Wave-1 deliverable, gates Wave 2)
- `docs/auth-architecture.md` (refresh semantics, state TTL, redaction, sandbox isolation)
- `docs/security-model.md` (env-var gates, destructive annotations, PII rules)
- `docs/release.md` (PyPI pending-publisher walkthrough, version semantics)
- `README.md` with `claude_desktop_config.json` example for macOS/Windows
- Fully passing pytest suite (read live + all writes replayed in CI; matrix 3 OS × 3 Python versions)
- GitHub Actions workflows: `ci.yml`, `release.yml`
- Initial git tag `v0.1.0` published to PyPI via OIDC

### Definition of Done
- [ ] `pypi.org/project/<package-name>/` returns 200 with v0.1.0 sdist + wheel
- [ ] `uvx <package-name> --version` prints `0.1.0` on macOS, Ubuntu, Windows (Wave-5 CI smoke)
- [ ] Claude Desktop using the example `claude_desktop_config.json` lists ALL MCP tools without error
- [ ] All 17 mandatory acceptance-criteria pytest cases (from Metis) pass in CI
- [ ] `TIKTOK_MCP_ALLOW_WRITES` env var hard-blocks every write tool when unset (parametrized test passes)
- [ ] Final Verification Wave F1-F4 all return APPROVE

### Must Have
- Manual-paste OAuth flow with state TTL + PKCE
- Per-account `asyncio.Lock` token refresh
- `BusinessApiResponse` envelope decoder shared across Business/Marketing/Comments
- `SecretRedactor` logging filter + httpx exception wrapper
- Two-step `remove_account` with confirmation_token
- Sandbox isolation in keychain namespacing
- Content Posting drafts-default (publish requires explicit `publish_immediately=True`)
- Native pagination passthrough per API
- Reactive rate-limit handling (Retry-After + exp backoff + max 3 retries)
- Three Wave-1 gating spikes: S1 (redirect fidelity), S2 (PyPI OIDC bootstrap), S3 (vcrpy `code != 0` fidelity)
- All 17 Metis-mandated pytest cases

### Must NOT Have (Guardrails)
- No localhost / no loopback OAuth capture (TikTok forbids)
- No hosted relay infrastructure (user explicitly declined)
- No separate CLI subcommands for setup (MCP tools only)
- No on-disk caching or persistence of comment text
- No silent overwrite of app credentials (user must call `set_app_credentials` explicitly)
- No silent deletion of broken account tokens (mark broken, surface to user, do NOT auto-delete)
- No `list_app_credentials` ever returning raw secret values (fingerprints + booleans only)
- No auto-publish of videos (default = draft inbox; explicit opt-in required)
- No cross-currency aggregation in reporting tools (v0.2 candidate)
- No unified pagination abstraction (native passthrough only)
- No live HTTP in write-tool tests in CI (replay-only)
- No logging to stdout (reserved for MCP protocol; stderr only)
- No `as any` / `@ts-ignore` equivalents (no `typing.cast` to bypass type errors)
- No `print()` statements in committed code (use logger)
- No generic identifiers (`data`, `result`, `item`, `temp`) in committed code (AI-slop avoidance)
- No `except Exception: pass` (must log + propagate or handle specifically)
- No commented-out code blocks at PR merge time
- No JSDoc/docstring-on-every-line clutter
- No abstraction layers without proven need (no `BaseTool` ABC unless 5+ tools share state)

---

## Verification Strategy (MANDATORY)

> **ZERO HUMAN INTERVENTION** in acceptance criteria. Every check is an executable command or pytest case.

### Test Decision
- **Infrastructure exists**: NO (greenfield) — Wave 1 sets it up
- **Automated tests**: YES (TDD)
- **Framework**: `pytest` + `pytest-asyncio` + `vcrpy` + `freezegun`/`time-machine`
- **TDD flow**: RED (failing test with vcrpy cassette or mocked httpx response) → GREEN (minimal implementation) → REFACTOR (extract patterns)

### QA Policy
Every task MUST include agent-executed QA scenarios with concrete commands and evidence paths. Evidence saved to `.omo/evidence/task-{N}-{slug}.{ext}`.

- **MCP tool behaviour**: spawn the stdio MCP via `uv run tiktok-mcp` from a pytest fixture; send JSON-RPC frames; assert responses (use the MCP SDK in-memory transport for unit tests where possible).
- **API integration**: pytest with vcrpy cassettes recorded against TikTok sandbox; cassettes committed to `tests/cassettes/` with comment-body scrubbing for comment endpoints.
- **CLI / packaging**: tmux session via `interactive_bash` to run `uvx --from . tiktok-mcp --version` and verify exit code + stdout.
- **GitHub Actions**: `act` (locally) + actual PR with workflow runs as proof.
- **claude_desktop_config.json**: spawn MCP using the exact config block; send `tools/list`; assert response shape.

---

## Execution Strategy

### Parallel Execution Waves

> Maximize throughput by grouping independent tasks into parallel waves. Each wave completes before the next begins.
> Target: 5-8 tasks per wave. Three Wave-0 gating spikes run first.

```
Wave 0 (GATING — run BEFORE anything else; project-killing risks):
├── S1: Redirect-URL fidelity spike (manual-paste flow viability via oauth.example.com)
├── S2: PyPI OIDC pending-publisher bootstrap (release pipeline viability)
└── S3: vcrpy fidelity on HTTP 200 + code != 0 Business API errors

**Note on wave sizing (Oracle phase-2 review)**: Waves 1, 2, 3 each contain 9-10 tasks rather than the 5-8 target. This is INTENTIONAL and reflects the dependency reality below:

- **Wave 1** (9 tasks T1-T9): Foundation tasks have an INTERNAL dependency staircase, NOT full parallelism. The Dependency Matrix is the source of truth; this is the wave-level summary:
  - **Sub-wave 1.0** (parallel-start): T1 (pyproject + hatchling scaffolding) — MUST land first.
  - **Sub-wave 1.1** (after T1): T2 (project layout) — strictly needed by T3-T9.
  - **Sub-wave 1.2** (after T2, parallel start): T3 (types), T4 (redactor), T6 (state), T9 (API surface inventory doc) — these 4 have no further Wave-1 prerequisites.
  - **Sub-wave 1.3** (after T3 and T4 land): T5 (keychain — depends on T3 types), T7 (envelope decoders — depend on T3 types + T4 redactor), T8 (write-gate decorator — depends on T3 types).
  - Net effect: 9 Wave-1 tasks resolve in 4 sub-wave passes (T1 → T2 → T3/T4/T6/T9 in parallel → T5/T7/T8 in parallel once T3/T4 land). We KEEP the label "Wave 1" because the sub-wave boundaries are shallow and the Dependency Matrix already encodes the precise execution graph. Splitting into 4 named waves would be ceremonial overhead, not parallelism gain.

- **Wave 2** (10 tasks T10-T19): Same pattern — T10/T11 (auth account tools) can start in parallel with T12/T14/T18 (API clients). T13/T15/T16/T17/T19 are read tools that depend on their respective clients. Two sub-waves (clients → read tools) but kept as one labeled Wave for sequencing clarity.

- **Wave 3** (9 tasks T20-T28): Write tools across four APIs. Each write tool depends on the corresponding Wave 2 client. All 9 Wave-3 tasks are mutually independent — T28 (draft management) does NOT depend on T26 (chunked video upload) at the task level: drafts can exist independently (created by either T26 chunked upload or T27 pull-from-URL, OR pre-existing from prior user-side uploads). Splitting by API would create 4 micro-waves with 2-3 tasks each — strictly worse for synchronization. Wave 3 fans out maximally.

The 5-8 target applies when dependencies map naturally to that batch size. Our foundation primitives map naturally to 7+2 (T1 alone, T2 alone, then T3-T9). We label these as one wave because the sub-wave boundaries are shallow and the Dependency Matrix already encodes the true ordering.

Wave 1 (Foundation — after spikes pass):
├── 1. pyproject.toml + hatchling + hatch-vcs scaffolding
├── 2. Project layout (src/tiktok_mcp/{__init__,server,cli,...}.py + tests/)
├── 3. Core types module (pydantic v2 models for OAuth, accounts, app creds, errors)
├── 4. SecretRedactor logging filter + httpx exception wrapper
├── 5. Keychain backend abstraction (keyring + cryptography.fernet fallback)
├── 6. OAuth state manager (in-memory dict + TTL + single-use)
├── 7. BusinessApiResponse + DisplayApiResponse envelope decoders
├── 8. require_writes_enabled decorator + env-var parser
└── 9. API surface inventory deliverable (docs/api-surface-inventory.md)

Wave 2 (Auth subsystem + read tools across 4 APIs — MAX PARALLEL):
├── 10. add_account / complete_account_login / list_accounts / rename_account / remove_account tools
├── 11. set_app_credentials / list_app_credentials / verify_app_credentials tools
├── 12. Display API client (httpx + auth + retry + rate limit)
├── 13. Display API read tools (get_user_info, list_videos, query_videos, get_video_metrics)
├── 14. Business API client (httpx + auth + BusinessApiResponse envelope)
├── 15. Marketing API read tools (list_advertisers, list_campaigns, list_adgroups, list_ads, get_advertiser_info)
├── 16. Marketing API report tools (run_sync_report, run_async_report, poll_async_report, download_async_report)
├── 17. Business Organic comment read tools (list_comments, list_replies, get_comment)
├── 18. Content Posting API read tools (get_post_status, list_drafts, get_creator_info)
└── 19. get_rate_limit_status tool

Wave 3 (Write tools across 4 APIs — all gated by env var):
├── 20. Marketing API campaign writes (create_campaign, update_campaign, pause_campaign, resume_campaign, delete_campaign)
├── 21. Marketing API adgroup writes (create/update/pause/resume/delete_adgroup)
├── 22. Marketing API ad writes (create/update/pause/resume/delete_ad)
├── 23. Marketing API audience uploads (upload_custom_audience, list_audiences)
├── 24. Marketing API creative uploads (upload_video_creative, upload_image_creative, list_creatives)
├── 25. Business Organic comment moderation writes (post_reply, pin_comment, unpin_comment, hide_comment, unhide_comment, delete_comment)
├── 26. Content Posting writes — video upload chunked (init_video_upload, upload_video_chunk, finalize_video_upload)
├── 27. Content Posting writes — pull-from-URL + photo (publish_from_url, publish_photo)
└── 28. Content Posting writes — draft management (move_to_drafts, publish_draft, delete_draft)

Wave 4 (Resources + prompts + polish):
├── 29. MCP Resources (accounts:// resource enumerating accounts)
├── 30. MCP Prompts (templates for common workflows: "weekly ads report", "comment moderation queue")
├── 31. tools/list ordering + tool descriptions polish
├── 32. End-to-end stdio MCP boot test (claude_desktop_config.json validation)
├── 33. README.md with claude_desktop_config.json examples for macOS + Windows
├── 34. docs/auth-architecture.md (refresh flow, state TTL, sandbox isolation diagrams)
├── 35. docs/security-model.md (env-var gates, destructive annotations, PII rules)
└── 36. docs/release.md (pending-publisher walkthrough + version semantics)

Wave 5 (CI + release pipeline — does NOT ship the release; T37-T41 only):
├── 37. .github/workflows/ci.yml (matrix 3 OS × 3 Python, lint + type + test + smoke)
├── 38. .github/workflows/release.yml (tag trigger, OIDC publish, fetch-depth: 0 for hatch-vcs)
├── 39. PyPI pending-publisher registration documentation + first-publish dry run on TestPyPI
├── 40. release-please / git-cliff CHANGELOG automation (single choice based on research)
└── 41. Distribution smoke: uvx install + run on fresh macOS/Linux/Windows VMs (CI matrix)

Wave FINAL (after Wave 5 — 4 parallel reviews; MUST all APPROVE before Wave 6):
├── F1. Plan Compliance Audit (oracle)
├── F2. Code Quality Review (unspecified-high)
├── F3. Real Manual QA (unspecified-high)
└── F4. Scope Fidelity Check (deep)

Wave 6 (TERMINAL release — runs ONLY after F1-F4 all APPROVE):
└── 42. Initial v0.1.0 tag + production release (this is the agent's "ship it" gate; F1-F4 are ticked first, then T42 pushes the tag)

Critical Path: S1 → 1, 5, 6 → 10 → 12, 14 → 13, 15-18 → 20-28 → 32 → 37-41 → F1-F4 (APPROVE) → 42
Parallel speedup: ~70%+ over sequential (concurrent waves of 5-9 tasks)
Max concurrent: 9 (Wave 2)
```

### Dependency Matrix (abbreviated)

Full matrix populated as tasks are appended. Key dependencies:
- **S1, S2, S3 (Wave 0)** block ALL Wave 1+ tasks
- **Task 1 (pyproject scaffold)** blocks all other Wave 1 tasks
- **Tasks 4-8 (foundations: redactor, keychain, state, envelopes, write gate)** block all Wave 2 tools
- **Task 9 (inventory)** blocks Wave 3 (write tools — without inventory, "full featureset" is uncomputable)
- **Tasks 10-11 (account + app cred tools)** block all read/write tools (they need accounts to use)
- **Tasks 12, 14 (clients)** block all Display/Marketing/Business read+write tools
- **Tasks 13-18 (read tools)** block Wave 3 write tools (write tools reuse client patterns; build read first to test client)
- **Tasks 20-28 (writes)** block Wave 4 polish (need final tool set for tools/list ordering)
- **Tasks 32-36 (polish + docs)** block Wave 5 release (README + docs must be in repo before tag)
- **Task 38 (release.yml)** blocks Task 42 (v0.1.0 release)

### Agent Dispatch Summary

- **Wave 0**: 3 spikes — S1 → `deep`, S2 → `deep`, S3 → `unspecified-high`
- **Wave 1**: 9 tasks (4 sub-wave passes) — T1-T2 → `quick`, T3-T8 → `unspecified-high`, T9 → `writing`
- **Wave 2**: 10 tasks — T10-T11 → `unspecified-high`, T12, T14, T18 → `unspecified-high` (clients), T13, T15-T17, T19 → `unspecified-high` (read tools)
- **Wave 3**: 9 tasks — all → `unspecified-high`
- **Wave 4**: 8 tasks — T29-T32, T36 → `unspecified-high`, T33-T35 → `writing`
- **Wave 5**: 5 tasks (CI + release pipeline, does NOT ship) — T37-T38, T41 → `unspecified-high`, T39 → `writing`, T40 → `unspecified-high`
- **Wave FINAL**: 4 reviewers — F1 → `oracle`, F2 → `unspecified-high`, F3 → `unspecified-high`, F4 → `deep`
- **Wave 6**: 1 task (terminal release, runs ONLY after F1-F4 all APPROVE) — T42 → `unspecified-high`

---

## TODOs

> Tasks are populated by subsequent Edit-append operations to honor the incremental-write protocol.
> Each task has: What to do / Must NOT do / Recommended Agent / Parallelization / References / Acceptance Criteria with Agent-Executed QA Scenarios / Commit.

### Wave 0 — GATING SPIKES (must complete before Wave 1)

> If ANY of these returns a blocking outcome, halt and consult the user before proceeding. These spikes verify project-killing assumptions.

- [~] S1. **Spike: TLD redirect URL fidelity (manual-paste flow viability)** → BLOCKED EXTERNAL — see .omo/evidence/operator-unblock-checklist.md (agent artifacts complete; operator must run `python spikes/s1_redirect.py` with sandbox credentials in the 6 TIKTOK_S1_* env vars to produce PASS/PARTIAL/FAIL verdict in spikes/s1_results.md)

  > **NOTE: Spike S1 contains OPERATOR-REQUIRED steps** (browsing to TikTok's real auth UI and pasting back the redirect URL). These are the unavoidable cost of validating TikTok's actual redirect behavior. The spike treats those operator steps as a one-time pre-implementation gate, NOT as ongoing automated acceptance criteria. Evidence is captured by recording the operator's browser session via Playwright's `playwright codegen` or by manual screenshot — both are acceptable here. After the spike validates the redirect, ALL subsequent tasks (T1-T42) use automated QA only.

  **What to do**:
  - Open a Python REPL or write a throwaway `spikes/s1_redirect.py` script. NOT committed to `src/`.
  - For BOTH the Display API sandbox app AND the Business API sandbox app:
    1. Build the OAuth authorization URL with `state=<random>` + (where applicable) PKCE `code_challenge`.
    2. `webbrowser.open()` the URL in the local browser.
    3. Authenticate manually with a sandbox-allowlisted TikTok account.
    4. Observe TikTok's redirect — capture the FULL final URL from the browser address bar.
    5. Manually parse the URL with `urllib.parse` and verify: `code` is present and non-empty, `state` matches exactly what was sent, host matches the registered redirect URI, no extra suspicious params.
    6. Exchange the `code` for an access token via the documented token endpoint. Verify a successful token response.
  - Document the result per API in `spikes/s1_results.md`: registered URI, captured URL pattern, params preserved/lost, token-exchange success.
  - **Decision branch**:
    - **PASS** (both flows work): manual-paste flow validated; the rest of the plan proceeds.
    - **PARTIAL** (one flow degrades — e.g. TikTok appends extra params): document the degradation; the `complete_account_login` parser must be robust enough to ignore extras; plan proceeds with note.
    - **FAIL** (TikTok strips `code` or `state`, or refuses the redirect host, or token exchange impossible): PROJECT-KILLING. Halt; resurface to user with alternatives (hosted relay, separate app with different redirect, abandon Business API surface).

  **Must NOT do**:
  - Do not commit working OAuth code into `src/` yet. This is a spike — throwaway only.
  - Do not log the captured `code` or any tokens to disk.
  - Do not test against production app credentials — sandbox only.

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Spike requires reasoned investigation, browser interaction, ambiguous outcome handling. Not a mechanical task.
  - **Skills**: none required (manual browser flow + Python urllib)

  **Parallelization**:
  - **Can Run In Parallel**: YES (with S2, S3)
  - **Parallel Group**: Wave 0 (with S2, S3)
  - **Blocks**: ALL Wave 1+ tasks
  - **Blocked By**: None — can start immediately

  **References**:

  **App credentials**:
  - Display API + Business API app credentials (production + sandbox) are available locally on the user's machine — values intentionally NOT documented in this plan; the implementing agent must read them at runtime from the user-provided file/keychain and never log or echo them.
  - Business API production app is registered with redirect_uri `https://oauth.example.com` (this is the registered HOST, not a callback service we control).
  - Business API sandbox has a pre-minted access_token for one test advertiser_id; the implementing agent must request the exact token from the user via a single MCP tool call, never paste it into a plan/draft/log.
  - Post-spike action: instruct the user to move the plaintext credentials file off Desktop into the OS keychain via the `set_app_credentials` tool, then delete the plaintext file.

  **OAuth endpoints (Display API / Login Kit)**:
  - Authorization: `https://www.tiktok.com/v2/auth/authorize/` (params: `client_key`, `scope`, `response_type=code`, `redirect_uri`, `state`, optional `code_challenge` + `code_challenge_method=S256` for desktop)
  - Token exchange: `https://open.tiktokapis.com/v2/oauth/token/` (params: `client_key`, `client_secret`, `code`, `grant_type=authorization_code`, `redirect_uri`, optional `code_verifier`)

  **OAuth endpoints (Business API)**:
  - Authorization: `https://business-api.tiktok.com/portal/auth?app_id=<app_id>&state=<state>&redirect_uri=<encoded>`
  - Token exchange: `https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/` (POST JSON body: `app_id`, `secret`, `auth_code`)

  **WHY each reference matters**:
  - The auth URL constructions differ between Login Kit (Display + Content Posting) and Business API; the spike must exercise BOTH to validate the manual-paste flow against the union of registered redirect URIs.
  - The Business API token endpoint expects POST JSON, not form-encoded — common gotcha.

  **Acceptance Criteria**:
  - [ ] `spikes/s1_results.md` exists and documents per-API outcome (Display + Business)
  - [ ] At least one of the captured redirect URLs successfully exchanged for a token in both flows
  - [ ] Decision branch declared in writing: PASS / PARTIAL / FAIL
  - [ ] No tokens or codes logged or committed
  - [ ] `git status` shows ONLY `spikes/s1_redirect.py` + `spikes/s1_results.md` as additions; no `src/` changes

  **Operator Pre-Spike Steps (NOT part of agent acceptance criteria)**:

  > These steps are inherently operator-driven (they validate TikTok's real-world redirect behavior in a live browser). They are captured here for completeness but live OUTSIDE the QA Scenarios block. Per AGENTS.md, blocking operator steps are handled via the operator-unblock-checklist mechanism (`.sisyphus/evidence/operator-unblock-checklist.md`) and the agent merely VERIFIES the resulting artifacts. The agent does NOT itself execute these steps.
  >
  > 1. Operator runs `python spikes/s1_redirect.py --api display --action open` — script prints auth URL.
  > 2. Operator opens URL in browser, authenticates with a sandbox-allowlisted TikTok account, consents.
  > 3. Operator copies the full redirect URL from the browser address bar.
  > 4. Operator runs `python spikes/s1_redirect.py --api display --action exchange --url '<URL>'`.
  > 5. Repeat for `--api business`.
  > 6. Operator authors `spikes/s1_results.md` per the schema described in "What to do" above (per-API: PASS/PARTIAL/FAIL, params preserved, token-exchange outcome).
  >
  > The agent's automated QA below verifies the artifacts produced by these operator steps.

  **QA Scenarios (MANDATORY — fully agent-executable)**:

  ```
  Scenario: spikes/s1_results.md artifact validates schema
    Tool: Bash (python verification script)
    Preconditions: Operator pre-spike steps complete; spikes/s1_results.md exists on disk
    Steps:
      1. Run: `python -c "
import sys, re, pathlib
p = pathlib.Path('spikes/s1_results.md')
if not p.exists():
    sys.exit('FAIL: spikes/s1_results.md not found')
text = p.read_text()
required = ['## Display API', '## Business API', 'verdict:', 'params_preserved:', 'token_exchange:']
missing = [r for r in required if r not in text]
if missing:
    sys.exit(f'FAIL: missing sections {missing}')
# Verdict per API must be one of PASS/PARTIAL/FAIL
verdicts = re.findall(r'verdict:\s*(PASS|PARTIAL|FAIL)', text)
if len(verdicts) < 2:
    sys.exit(f'FAIL: expected 2 verdicts (Display + Business), got {len(verdicts)}')
# No tokens leaked
if re.search(r'[A-Za-z0-9_-]{40,}', text):
    sys.exit('FAIL: artifact contains suspicious long token-like string — sanitize before committing')
print('OK')
"`
    Expected Result: stdout "OK", exit code 0
    Failure Indicators: missing sections, missing verdicts, leaked tokens
    Evidence: .omo/evidence/task-S1-artifact-validation.txt

  Scenario: Pasted URL parser robustness (fully automated — does NOT require live OAuth)
    Tool: Bash (python REPL)
    Preconditions: spikes/s1_redirect.py exists; script's `parse_redirect_url(raw)` is importable
    Steps:
      1. Run: `python -c "
from spikes.s1_redirect import parse_redirect_url
test_url = 'https://oauth.example.com/?code=ABC&state=XYZ'
cases = [
    test_url,
    f'  {test_url}  ',  # whitespace
    f'\"{test_url}\"',  # quotes
    f'\`{test_url}\`',  # backticks
    f'[click here]({test_url})',  # markdown link
    f'{test_url}\n',  # trailing newline
]
for raw in cases:
    parsed = parse_redirect_url(raw)
    assert parsed['code'] == 'ABC' and parsed['state'] == 'XYZ', f'failed on: {raw!r}'
print('OK — 6 URL-shape variations parsed correctly')
"`
    Expected Result: stdout "OK — 6 URL-shape variations parsed correctly", exit 0
    Failure Indicators: AssertionError on any variant
    Evidence: .omo/evidence/task-S1-parser-robustness.txt

  Scenario: spikes/ directory does not leak into src/ or tests/
    Tool: Bash
    Preconditions: spike work complete
    Steps:
      1. Run: `git diff --name-only HEAD~1 HEAD | grep -E '^src/|^tests/' | wc -l`
    Expected Result: 0 (spike must not contaminate production code)
    Failure Indicators: any src/ or tests/ files in spike commits
    Evidence: .omo/evidence/task-S1-isolation.txt
    Evidence: .omo/evidence/task-S1-paste-robust.txt (parser unit test output)

  Scenario: Decision branch declared
    Tool: read
    Preconditions: spike complete
    Steps:
      1. Read spikes/s1_results.md
    Expected Result: file contains exact tokens "DECISION: PASS" or "DECISION: PARTIAL" or "DECISION: FAIL" with rationale
    Evidence: spikes/s1_results.md
  ```

  **Commit**: YES (groups with S2, S3 — single "Wave 0 spikes" commit at wave close)
  - Message: `chore(spike): verify TLD redirect manual-paste flow for Display + Business APIs`
  - Files: `spikes/s1_redirect.py`, `spikes/s1_results.md`
  - Pre-commit: `git diff --stat` shows only `spikes/` additions

- [~] S2. **Spike: PyPI OIDC trusted-publishing pending-publisher bootstrap** → BLOCKED EXTERNAL — see .omo/evidence/operator-unblock-checklist.md (agent scaffolding + workflow complete; operator must check package-name availability, register TestPyPI pending publisher, create GitHub Environment `pypi`, push `vSPIKE-0.0.0a0` tag, fill spikes/s2_results.md verdict)

  **What to do**:
  - Reserve the chosen PyPI package name (`tiktok-mcp` first; if 404 → `tiktok-complete-mcp`). Document availability check in `spikes/s2_results.md`.
  - Configure **TestPyPI** pending publisher (NOT production PyPI yet):
    1. Register the package name with a pending publisher on TestPyPI under `signikant`'s account (or whichever account user designates), pointing to the GitHub repo `<owner>/tiktok-mcp`, workflow file `release.yml`, environment `pypi`.
    2. Create the GitHub repository environment `pypi` with optional protection rules (e.g. required reviewers off for spike; can tighten later).
    3. Create a throwaway `spikes/release-spike/` directory with a minimum `pyproject.toml` (`hatchling` build backend, version `0.0.0a0`, name = chosen PyPI name, single `src/tiktok_mcp/__init__.py`).
    4. Create a throwaway workflow `.github/workflows/release-spike.yml` that triggers on tag `vSPIKE-*`, builds with `uv build` (or `python -m build`), publishes to TestPyPI via `pypa/gh-action-pypi-publish@release/v1` with `repository-url: https://test.pypi.org/legacy/`.
    5. Push tag `vSPIKE-0.0.0a0`; verify workflow run succeeds; verify `pip index versions <name> -i https://test.pypi.org/simple/` returns `0.0.0a0`.
  - Document the EXACT 5-step pre-creation dance in `spikes/s2_results.md` so production setup (Wave 5) follows the proven recipe.
  - Yank the TestPyPI release immediately after verification (so the name stays clean for v0.1.0).

  **Must NOT do**:
  - Do NOT publish to production PyPI in this spike.
  - Do NOT commit the throwaway `spikes/release-spike/` content into the main package layout — keep isolated.
  - Do NOT use a PyPI API token (the whole point is OIDC; trust the OIDC flow).

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Multi-step infra setup with external services (PyPI account, GitHub Actions, environments) — needs careful state tracking.
  - **Skills**: none required (web UI + Actions YAML)

  **Parallelization**:
  - **Can Run In Parallel**: YES (with S1, S3)
  - **Parallel Group**: Wave 0
  - **Blocks**: Wave 5 release pipeline tasks
  - **Blocked By**: None

  **References**:

  **External docs**:
  - PyPI pending publishers: `https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/`
  - GitHub Action: `https://github.com/pypa/gh-action-pypi-publish` (use `release/v1` ref; check README for current `id-token: write` snippet)
  - hatchling: `https://hatch.pypa.io/latest/config/build/` (build-system requires + project metadata)
  - hatch-vcs: `https://github.com/ofek/hatch-vcs` (tag-driven version pattern)

  **Internal context**:
  - The user's GitHub account / organization that will host this repo — confirm with user before configuring environment

  **WHY each reference matters**:
  - Pending-publisher flow is non-obvious: register BEFORE the package exists on PyPI, then first publish completes the binding. Getting the sequence wrong causes a "no project found" 4xx loop.
  - `id-token: write` permission must be set at job level, not workflow level, for OIDC to work — easy to mis-place.

  **Acceptance Criteria**:
  - [ ] `spikes/s2_results.md` documents the exact 5-step bootstrap sequence with the values used
  - [ ] `pip index versions <chosen-name> -i https://test.pypi.org/simple/` returns at least one version
  - [ ] GitHub workflow run for `vSPIKE-0.0.0a0` shows conclusion=success (verify via `gh run view`)
  - [ ] TestPyPI release yanked immediately after verification (`pip index` may still show it but unyielded)
  - [ ] No PyPI API token committed or stored anywhere

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: TestPyPI pending publisher completes binding on first publish (happy path)
    Tool: Bash + gh CLI
    Preconditions: GitHub repo created at the chosen path; TestPyPI account exists; pending publisher registered with: project name=<name>, owner=<gh-user>, repo=tiktok-mcp, workflow=release-spike.yml, env=pypi
    Steps:
      1. git push origin main  # establishes baseline
      2. git tag vSPIKE-0.0.0a0 && git push origin vSPIKE-0.0.0a0
      3. gh run watch --exit-status   # wait for workflow to complete
      4. curl -fsSL https://test.pypi.org/pypi/<name>/json | jq '.releases | keys'
    Expected Result: workflow exit 0; jq output includes "0.0.0a0"; pending publisher converted to active
    Failure Indicators: workflow fails on "id_token" step → check `id-token: write` placement; "no project found" → pending publisher mis-configured
    Evidence: .omo/evidence/task-S2-testpypi-bind.txt (gh run log + jq output)

  Scenario: Verify OIDC token issuance + no API token in workflow
    Tool: Bash
    Preconditions: workflow file exists at .github/workflows/release-spike.yml
    Steps:
      1. grep -E "secrets\\.[A-Z_]*TOKEN" .github/workflows/release-spike.yml
      2. grep "id-token: write" .github/workflows/release-spike.yml
    Expected Result: grep #1 returns no matches (no token secrets used); grep #2 returns at least one match at job level
    Evidence: .omo/evidence/task-S2-no-token.txt

  Scenario: Verify package-name availability decision recorded
    Tool: Bash + jq
    Preconditions: spike running
    Steps:
      1. curl -sf https://pypi.org/pypi/tiktok-mcp/json -o /dev/null && echo TAKEN || echo AVAILABLE
      2. read spikes/s2_results.md and assert it documents the result
    Expected Result: file contains either "PyPI name: tiktok-mcp (AVAILABLE)" or "PyPI name: tiktok-complete-mcp (FALLBACK — tiktok-mcp TAKEN)"
    Evidence: spikes/s2_results.md
  ```

  **Commit**: YES (groups with S1, S3)
  - Message: `chore(spike): verify PyPI OIDC trusted-publisher bootstrap via TestPyPI`
  - Files: `spikes/s2_results.md`, `spikes/release-spike/` (kept for reference; pruned in Wave 5 cleanup), `.github/workflows/release-spike.yml` (kept until v0.1.0 release, then removed)
  - Pre-commit: workflow run on spike tag passed

- [~] S3. **Spike: vcrpy fidelity on `HTTP 200 + code != 0` Business API errors** → BLOCKED EXTERNAL — see .omo/evidence/operator-unblock-checklist.md (agent script + prototype decoder + skip-aware tests complete; operator must export TIKTOK_BUSINESS_SANDBOX_TOKEN and run `uv run --with httpx --with vcrpy --with pytest pytest spikes/test_s3_vcr.py::test_record` to record cassette, then verify scrub + determinism, fill spikes/s3_results.md verdict)

  **What to do**:
  - Write a throwaway `spikes/s3_vcr.py` that:
    1. Uses the sandbox Business API access_token to deliberately call an endpoint with an INVALID parameter (e.g. `GET /open_api/v1.3/advertiser/info/?advertiser_ids=["doesnotexist"]` or omit a required arg).
    2. Records the HTTP exchange with `vcrpy` into `spikes/cassettes/s3_business_error.yaml`.
    3. Confirms the response is `HTTP 200 OK` with body `{ "code": <nonzero>, "message": "...", "request_id": "..." }`.
  - Write a throwaway pytest `spikes/test_s3_vcr.py` that:
    1. Replays the cassette via vcrpy's `@vcr.use_cassette`.
    2. Invokes a minimal `BusinessApiResponse` envelope decoder (which is a Wave-1 deliverable; here a tiny prototype suffices).
    3. Asserts that the decoder raises `BusinessApiError(code=<expected>, request_id=<expected>)`.
  - Verify the cassette is replayable: `python -m pytest spikes/test_s3_vcr.py -v` passes.
  - Document in `spikes/s3_results.md`: vcrpy version used, cassette format, any header/body scrubbing required, decoder pattern that worked.

  **Must NOT do**:
  - Do NOT commit the prototype decoder into `src/` — Wave 1 builds the real one. Spike code stays in `spikes/`.
  - Do NOT include real tokens in the cassette. Use vcrpy's `filter_headers=[("Authorization", "REDACTED")]` and `before_record_response` body sanitization.
  - Do NOT use production credentials.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Methodical but well-defined. Standard Python tooling.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with S1, S2)
  - **Parallel Group**: Wave 0
  - **Blocks**: Wave 1 task 7 (BusinessApiResponse decoder) — the spike validates the approach before the production decoder is built
  - **Blocked By**: None

  **References**:

  **vcrpy**:
  - `https://vcrpy.readthedocs.io/en/latest/usage.html` — basic recording, replay, filtering
  - `filter_headers` + `before_record_response` for scrubbing
  - Recorder mode `"once"` for first run, `"none"` for CI replay-only

  **Business API error envelope (confirmed in research)**:
  - HTTP 200 + JSON body `{ "code": <int>, "message": <str>, "data": <object>, "request_id": <str> }`
  - `code == 0` means success; any non-zero code is a business error
  - `request_id` is the correlation id for TikTok support escalation

  **WHY each reference matters**:
  - vcrpy's default scrubbing does NOT remove tokens — must configure explicitly. A leaked cassette has been a real-world breach pattern.
  - The Business API envelope is the single biggest divergence from REST norms in TikTok's stack; getting the spike right de-risks the entire Wave-2 Business client.

  **Acceptance Criteria**:
  - [ ] `spikes/cassettes/s3_business_error.yaml` exists, is committable (no secrets via `grep -E "Bearer [a-z0-9]{32,}"` returns nothing)
  - [ ] `python -m pytest spikes/test_s3_vcr.py -v` returns exit 0 with 1 passed
  - [ ] `spikes/s3_results.md` documents: vcrpy version, scrub config used, decoder prototype that worked, any gotchas
  - [ ] Cassette replay produces the same `BusinessApiError(code=..., request_id=...)` deterministically on 10 consecutive runs

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: vcrpy records + replays HTTP 200 + code != 0 deterministically (happy path)
    Tool: Bash (pytest)
    Preconditions: vcrpy installed; sandbox Business API access_token in env var TIKTOK_BUSINESS_SANDBOX_TOKEN
    Steps:
      1. rm -f spikes/cassettes/s3_business_error.yaml  # fresh record
      2. TIKTOK_VCR_RECORD_MODE=once python -m pytest spikes/test_s3_vcr.py -v -k record
      3. Assert cassette file created, size > 0
      4. python -m pytest spikes/test_s3_vcr.py -v -k replay  # uses cassette
      5. for i in 1..10; do python -m pytest spikes/test_s3_vcr.py -v -k replay; done   # determinism
    Expected Result: 1 passed in steps 2-4; 10 of 10 passes in step 5
    Evidence: .omo/evidence/task-S3-determinism.txt (pytest output)

  Scenario: Token scrubbing in cassette (mandatory security check)
    Tool: Bash (grep)
    Preconditions: cassette recorded
    Steps:
      1. grep -E "Bearer [A-Za-z0-9_-]{20,}" spikes/cassettes/s3_business_error.yaml
      2. grep -E "access_token['\"]\\s*:\\s*['\"][A-Za-z0-9_-]{20,}" spikes/cassettes/s3_business_error.yaml
    Expected Result: both greps return no matches (exit code 1)
    Evidence: .omo/evidence/task-S3-scrub.txt (grep output)

  Scenario: Decoder raises typed BusinessApiError on code != 0
    Tool: Bash (pytest)
    Preconditions: cassette + decoder prototype exist
    Steps:
      1. python -m pytest spikes/test_s3_vcr.py::test_decoder_raises_business_error -v
    Expected Result: 1 passed; exception type matches `BusinessApiError`; exception has `code`, `message`, `request_id` attrs
    Evidence: .omo/evidence/task-S3-decoder.txt
  ```

  **Commit**: YES (groups with S1, S2)
  - Message: `chore(spike): verify vcrpy fidelity on Business API HTTP 200 + code != 0 envelope`
  - Files: `spikes/s3_vcr.py`, `spikes/test_s3_vcr.py`, `spikes/cassettes/s3_business_error.yaml`, `spikes/s3_results.md`
  - Pre-commit: pytest passes; scrub grep returns nothing

### Wave 1 — Foundation (after Wave 0 PASS)

- [x] 1. **pyproject.toml + hatchling + hatch-vcs scaffolding**

  **What to do**:
  - Create `pyproject.toml` with:
    - `[build-system]` requires `hatchling>=1.27`, `hatch-vcs>=0.5`; build-backend `hatchling.build`
    - `[project]` metadata: `name=<chosen from S2>`, dynamic `version`, `description`, `readme="README.md"`, `requires-python=">=3.11"`, `license="MIT"`, `license-files=["LICENSE"]`, `authors`, `classifiers` (Python 3.11/3.12/3.13, MIT), `urls` (Homepage, Repository, Issues, Changelog)
    - `[project.scripts]` exactly `tiktok-mcp = "tiktok_mcp.server:main"` (entry point name MUST match the PyPI package name dash-converted)
    - `[project.optional-dependencies]` extras: `dev` (ruff, mypy, pytest, pytest-asyncio, vcrpy, freezegun), `test` (pytest stack only)
    - `[tool.hatch.version]` source `vcs`; `[tool.hatch.build.hooks.vcs]` version-file `src/tiktok_mcp/_version.py`
    - `[tool.hatch.build.targets.wheel]` packages `["src/tiktok_mcp"]`
    - `[tool.hatch.build.targets.sdist]` include patterns covering `src/`, `tests/`, `README.md`, `LICENSE`, `pyproject.toml`
  - Add core runtime deps: `mcp[cli]>=1.10,<2`, `httpx>=0.27,<1`, `pydantic>=2.7,<3`, `pydantic-settings>=2.4,<3`, `keyring>=25,<26`, `cryptography>=42,<46`, `platformdirs>=4,<5`, `tenacity>=8.5,<10` (retry/backoff)
  - Add `LICENSE` (MIT, copyright Signikant + year)
  - Add `.gitignore` (Python standard + `.venv/`, `dist/`, `*.egg-info/`, `.coverage`, `.pytest_cache/`, `tests/cassettes/.local/`)
  - Add `.python-version` (3.11) for `uv` autopick
  - Verify: `uv sync --all-extras --dev` resolves clean; `uv run python -c "import tiktok_mcp"` succeeds after Task 2 creates the package dir

  **Must NOT do**:
  - Hardcode the version in `pyproject.toml` (must come from git tag via hatch-vcs)
  - Pin transitive dependencies (only direct deps with conservative `>=X,<Y` ranges)
  - Include `mcp[cli]` AND standalone `fastmcp` — pick one (official `mcp[cli]` per Decisions of Record)
  - Add a `[tool.setuptools]` or `[tool.poetry]` section — hatchling only

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Mechanical scaffolding with well-known patterns; minimal reasoning.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: NO (foundational; blocks all other Wave 1 tasks)
  - **Parallel Group**: Wave 1 head
  - **Blocks**: 2, 3, 4, 5, 6, 7, 8, 9
  - **Blocked By**: S2 (need PyPI name decision)

  **References**:

  **External**:
  - Hatchling docs: `https://hatch.pypa.io/latest/config/build/`
  - hatch-vcs: `https://github.com/ofek/hatch-vcs#readme`
  - PEP 621: `https://peps.python.org/pep-0621/`
  - PyPA writing-pyproject guide: `https://packaging.python.org/en/latest/guides/writing-pyproject-toml/`
  - uv project layout: `https://docs.astral.sh/uv/concepts/projects/init/`

  **Internal**:
  - `spikes/s2_results.md` — PyPI name choice + pending-publisher confirmation
  - `.omo/drafts/tiktok-mcp.md` Decisions section 15 (canonical packaging spec)

  **Acceptance Criteria**:
  - [ ] `uv sync --all-extras --dev` exits 0 (run from a clean clone)
  - [ ] `uv build` produces both `dist/*.whl` and `dist/*.tar.gz` without errors
  - [ ] `uv run python -c "from tiktok_mcp import __version__; print(__version__)"` returns a non-empty version string (works after a `git tag v0.0.0-dev0` for local testing)
  - [ ] `twine check dist/*` (via `uvx twine check`) exits 0
  - [ ] `grep -E '^version\\s*=\\s*"' pyproject.toml` returns no static-version line (must be dynamic)

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Clean install via uv sync (happy path)
    Tool: Bash
    Preconditions: Fresh checkout; no .venv; uv installed
    Steps:
      1. cd /Users/user/Repositories/tiktok-mcp && rm -rf .venv dist
      2. uv sync --all-extras --dev
      3. uv run python -c "import mcp, httpx, pydantic, keyring, cryptography, platformdirs, tenacity"
    Expected Result: step 2 exits 0; step 3 exits 0 (no ImportError)
    Evidence: .omo/evidence/task-1-uv-sync.txt

  Scenario: Tag-driven version via hatch-vcs (happy path)
    Tool: Bash
    Preconditions: Task 2 complete (src/tiktok_mcp/ exists with __init__.py importing _version)
    Steps:
      1. git tag v0.0.0-dev0
      2. uv build
      3. uv run python -c "from tiktok_mcp import __version__; print(__version__)"
      4. git tag -d v0.0.0-dev0
    Expected Result: step 3 prints "0.0.0.dev0" (PEP 440 normalized) or similar
    Evidence: .omo/evidence/task-1-version-from-tag.txt

  Scenario: twine check passes (sdist/wheel sanity)
    Tool: Bash
    Steps:
      1. uv build
      2. uvx twine check dist/*
    Expected Result: PASSED for both wheel and sdist
    Evidence: .omo/evidence/task-1-twine-check.txt

  Scenario: No static version in pyproject (negative check)
    Tool: Bash (grep)
    Steps:
      1. grep -nE '^version\\s*=' pyproject.toml || echo "OK: no static version"
    Expected Result: "OK: no static version"
    Evidence: .omo/evidence/task-1-no-static-version.txt
  ```

  **Commit**: YES
  - Message: `build: scaffold pyproject.toml with hatchling + hatch-vcs and core deps`
  - Files: `pyproject.toml`, `LICENSE`, `.gitignore`, `.python-version`
  - Pre-commit: `uv sync --all-extras --dev` and `uv build` both succeed

- [x] 2. **Project layout: src/tiktok_mcp/ + tests/ skeleton**

  **What to do**:
  - Create the source tree under `src/tiktok_mcp/`:
    ```
    src/tiktok_mcp/
      __init__.py            # exports __version__ from _version (generated by hatch-vcs)
      _version.py            # auto-generated; gitignored
      server.py              # FastMCP instance + main() entry point (initially just app.run(transport="stdio"))
      types/
        __init__.py
        accounts.py          # placeholder; populated in Task 3
        errors.py            # placeholder; populated in Task 3
        oauth.py             # placeholder; populated in Task 3
      auth/
        __init__.py
        state.py             # placeholder for Task 6
        keychain.py          # placeholder for Task 5
        redactor.py          # placeholder for Task 4
      api/
        __init__.py
        display/__init__.py
        marketing/__init__.py
        business/__init__.py
        posting/__init__.py
      tools/
        __init__.py          # tool registry; populated by Wave 2/3
      decorators.py          # placeholder for Task 8 (require_writes_enabled)
      envelopes.py           # placeholder for Task 7 (BusinessApiResponse + DisplayApiResponse)
    ```
  - Create `tests/` mirror:
    ```
    tests/
      __init__.py
      conftest.py            # shared fixtures (httpx mock, vcr config, env-var helpers)
      cassettes/             # vcrpy cassettes go here
      test_smoke.py          # imports tiktok_mcp; runs `tiktok-mcp --version`
      unit/__init__.py
      integration/__init__.py
    ```
  - `server.py` minimal contents: instantiate `FastMCP("tiktok-mcp")` named `app`; `def main() -> None: app.run(transport="stdio")`; `if __name__ == "__main__": main()`.
  - Add `tests/conftest.py` with: `pytest_asyncio` mode auto (`asyncio_mode = "auto"` in `pyproject.toml [tool.pytest.ini_options]`); `vcr_config` fixture pointing to `tests/cassettes/`; `clear_writes_env` fixture (autouse, unsets `TIKTOK_MCP_ALLOW_WRITES` per-test).
  - `pyproject.toml` additions: `[tool.pytest.ini_options]` with `addopts`, `asyncio_mode`, `testpaths`, `markers=["live: live HTTP tests", "live_write: live write HTTP tests (gated by TIKTOK_MCP_ALLOW_LIVE_WRITES)"]`. `[tool.ruff]` with line-length 100, target 3.11, lint rules `E,F,I,UP,B,SIM,N`. `[tool.mypy]` strict mode + `disallow_untyped_defs`.

  **Must NOT do**:
  - Implement business logic for any placeholder — only empty modules with `# populated in Task N` comments
  - Skip `__init__.py` files (namespace packages cause subtle import bugs)
  - Use flat layout (`tiktok_mcp/` at repo root) — must use src-layout

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Mechanical file/directory creation.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: NO (blocks T3-T9; T3-T9 all need src/tiktok_mcp/ to exist)
  - **Parallel Group**: Wave 1 head (sequential after T1)
  - **Blocks**: 3, 4, 5, 6, 7, 8
  - **Blocked By**: 1

  **References**:
  - PyPA src-layout: `https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/`
  - pytest-asyncio config: `https://pytest-asyncio.readthedocs.io/en/latest/concepts.html#auto-mode`
  - vcrpy fixture pattern: `https://vcrpy.readthedocs.io/en/latest/usage.html#use-with-pytest`
  - Decisions of Record (this plan) section 12 (redaction is mandatory) → file `auth/redactor.py` must exist as placeholder

  **Acceptance Criteria**:
  - [ ] `find src/tiktok_mcp -name '*.py' | wc -l` ≥ 15
  - [ ] `uv run python -m tiktok_mcp.server` exits cleanly (no exception) and prints nothing to stdout for at least 1 second (it's a stdio server waiting for input) — kill after 2s with timeout
  - [ ] `uv run pytest tests/ -v` runs (passes test_smoke.py; collects 0 other tests is fine for Task 2)
  - [ ] `uv run ruff check src/ tests/` exits 0
  - [ ] `uv run mypy src/` exits 0
  - [ ] `grep -r "FastMCP" src/tiktok_mcp/ | wc -l` ≥ 1 (server.py instantiates it)

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Smoke import + version (happy path)
    Tool: Bash
    Steps:
      1. uv run pytest tests/test_smoke.py -v
    Expected Result: 1 passed; test asserts tiktok_mcp.__version__ is a str
    Evidence: .omo/evidence/task-2-smoke.txt

  Scenario: MCP server starts on stdio without error
    Tool: Bash (timeout)
    Steps:
      1. timeout 2s uv run tiktok-mcp || EXIT=$? ; echo $EXIT
    Expected Result: $EXIT is 124 (timeout — server was running, killed by timeout). NOT non-124 (would mean crash).
    Evidence: .omo/evidence/task-2-stdio-boot.txt

  Scenario: Lint + type clean
    Tool: Bash
    Steps:
      1. uv run ruff check src/ tests/
      2. uv run mypy src/
    Expected Result: both exit 0
    Evidence: .omo/evidence/task-2-lint-type.txt
  ```

  **Commit**: YES
  - Message: `build: create src-layout package skeleton with tests/ and placeholder modules`
  - Files: src/tiktok_mcp/**, tests/**, pyproject.toml (pytest+ruff+mypy config additions)
  - Pre-commit: lint + type + smoke test all pass

- [x] 3. **Core types module: pydantic v2 models for OAuth, accounts, app creds, errors**

  **What to do**:
  - In `src/tiktok_mcp/types/accounts.py`:
    - Enum `ApiType` with members `DISPLAY`, `MARKETING`, `BUSINESS_ORGANIC`, `CONTENT_POSTING`
    - Enum `AccountStatus` with `OK`, `BROKEN`, `REFRESH_PENDING`, `REVOKED`
    - Pydantic model `Account` with: `alias: str` (validated: `^[a-z0-9-]{3,50}$`), `api_type: ApiType`, `sandbox: bool`, `tiktok_id: str` (open_id / advertiser_id / business_center_id depending on api_type), `display_name: str | None`, `avatar_url: str | None`, `scopes: list[str]`, `created_at: datetime`, `last_used_at: datetime | None`, `status: AccountStatus`. NO token fields — tokens live in a separate model never exported via `list_accounts`.
    - Pydantic model `AccountTokens` (private; never returned by any read tool): `access_token: SecretStr`, `refresh_token: SecretStr`, `access_token_expires_at: datetime`, `refresh_token_expires_at: datetime | None`, `last_rotated_at: datetime`.
    - Pydantic model `AccountWithTokens` for internal use only.
    - Pydantic model `AccountSummary` (return shape for `list_accounts`): subset of `Account` excluding `tiktok_id` raw value; includes `tiktok_id_fingerprint: str` (first 4 chars + length).
  - In `src/tiktok_mcp/types/oauth.py`:
    - Pydantic model `OAuthInProgress`: `state: str`, `api_type: ApiType`, `pkce_verifier: str | None`, `suggested_alias: str`, `expires_at: datetime`, `created_at: datetime`. (Used by Task 6.)
    - Pydantic model `OAuthAuthorizationUrl`: `url: str`, `state: str`, `suggested_alias: str`, `expires_at: datetime`. (Returned by `add_account` tool.)
    - Pydantic model `OAuthTokenResponse`: `access_token: SecretStr`, `refresh_token: SecretStr | None`, `expires_in: int`, `scope: list[str]`, `token_type: str = "Bearer"`, `open_id: str | None`, `advertiser_ids: list[str] | None`.
  - In `src/tiktok_mcp/types/app_credentials.py`:
    - Pydantic model `AppCredentials`: `api_type: ApiType`, `sandbox: bool`, `client_id: SecretStr` (Display uses `client_key`; rename normalized internally), `client_secret: SecretStr`, `created_at: datetime`. (Note: no `verified` / `last_verified_at` persisted state — verification is ephemeral per T11 design; see `AppCredentialsVerifyResult` below.)
    - Pydantic model `AppCredentialsSummary` (return shape for `list_app_credentials`): `api_type`, `sandbox`, `client_id_fingerprint: str` (first 4 + length, NEVER raw), `client_secret_set: bool`, `created_at`.
    - Pydantic model `AppCredentialsVerifyResult` (return shape for `verify_app_credentials`): `api_type`, `sandbox`, `client_id_fingerprint: str`, `valid: bool`, `verified_at: datetime` (timestamp of THIS verify call — ephemeral, never persisted), `error_code: str | None` (e.g. `"invalid_client"` if TikTok rejected), `error_message: str | None`. Returned BY VALUE only; not stored anywhere.
  - In `src/tiktok_mcp/types/errors.py`:
    - Base class `TikTokMCPError(Exception)` with `code: str` (machine-readable), `message: str`, `context: dict[str, Any]`.
    - Subclasses: `WritesDisabledError`, `KeychainLockedError`, `KeychainUnavailableError`, `AccountNotFoundError`, `AccountBrokenError`, `AppCredentialsNotSetError`, `OAuthStateInvalidError` (with subreasons `unknown`, `expired`, `consumed`, `replay`), `OAuthHostMismatchError`, `BusinessApiError` (with `code: int`, `message: str`, `request_id: str | None`), `DisplayApiError` (with `http_status: int`, `error_code: str | None`), `RateLimitedError` (with `retry_after: float | None`, `attempts: int`).
    - All subclasses serialize to the structured-error JSON shape used in `writes_disabled` response (see Decisions section 4).
  - Add `tests/unit/test_types.py` with: round-trip JSON serialization of each model; `AccountSummary.tiktok_id_fingerprint` shape correctness; `AppCredentialsSummary` never carries raw secrets even via `model_dump_json()`.

  **Must NOT do**:
  - Use plain `str` for any token, secret, or `client_secret` — always `SecretStr`
  - Expose `AccountTokens` via any return type from tool functions
  - Include token field names in any `*Summary` model
  - Use Optional[X] — prefer `X | None` (PEP 604, ruff `UP` rule)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Careful typed modelling with security implications (which fields are exported, which are private).
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T4, T5, T6, T7, T8 — they all depend on T2)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: 5 (keychain stores Account), 6 (state uses OAuthInProgress), 7 (envelopes raise BusinessApiError), 10 (account tools use these), 11 (app cred tools use these)
  - **Blocked By**: 2

  **References**:
  - pydantic v2 docs: `https://docs.pydantic.dev/latest/concepts/models/`
  - pydantic SecretStr: `https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr` (note: `model_dump_json` masks SecretStr by default — verify with a test)
  - PEP 604 union types: `https://peps.python.org/pep-0604/`
  - Decisions of Record section 12 (redaction) — `client_id_fingerprint` pattern is part of redaction-by-design

  **Acceptance Criteria**:
  - [ ] `uv run mypy src/tiktok_mcp/types/ --strict` exits 0
  - [ ] `uv run pytest tests/unit/test_types.py -v` ≥ 8 tests pass
  - [ ] `python -c "from tiktok_mcp.types.accounts import AccountSummary; from tiktok_mcp.types.oauth import OAuthInProgress; print('ok')"` exits 0
  - [ ] Searching the codebase: `grep -r "client_secret: str" src/` returns no matches (only `SecretStr`)
  - [ ] `AppCredentialsSummary(...).model_dump_json()` does NOT contain the secret value (assertion test)

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: SecretStr masks values in JSON serialization (security check)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_types.py::test_secret_masking -v
    Expected Result: 1 passed; test creates AppCredentials with known secret, calls model_dump_json(), asserts secret value NOT in output
    Evidence: .omo/evidence/task-3-secret-mask.txt

  Scenario: Round-trip serialization for every public model
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_types.py -v -k roundtrip
    Expected Result: ≥ 6 round-trip tests passed (one per public model: Account, AccountSummary, OAuthInProgress, OAuthAuthorizationUrl, AppCredentialsSummary, AppCredentialsVerifyResult). Note: AppCredentials itself is NOT public (only its Summary + VerifyResult are returned by tools); the test verifies the public-facing models only.
    Evidence: .omo/evidence/task-3-roundtrip.txt

  Scenario: Error classes serialize to spec'd structured shape
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_types.py::test_error_serialization -v
    Expected Result: pass; each error subclass `.to_dict()` returns {"error": <code>, "message": <msg>, "context": {...}} matching the Decisions section 4 shape
    Evidence: .omo/evidence/task-3-error-shape.txt
  ```

  **Commit**: YES
  - Message: `feat(types): add pydantic v2 models for accounts, OAuth, app credentials, errors`
  - Files: `src/tiktok_mcp/types/{accounts,oauth,app_credentials,errors}.py`, `tests/unit/test_types.py`
  - Pre-commit: mypy strict + pytest test_types all pass

- [x] 4. **SecretRedactor logging filter + httpx exception wrapper**

  **What to do**:
  - In `src/tiktok_mcp/auth/redactor.py`:
    - Class `SecretRedactor(logging.Filter)`:
      - Constructor accepts seed patterns (e.g. `["access_token", "refresh_token", "code", "client_secret", "auth_code", "secret", "Authorization", "Bearer"]`) as substring keys; on match, masks the VALUE following `<key>=`, `<key>:`, or `"<key>"\s*:\s*"<value>"` patterns.
      - Maintains a thread-safe `set[str]` of EXACT token values seeded at runtime by `register_token(token)` / `unregister_token(token)`. Filter scans log records for these exact substrings and replaces with `<REDACTED:token>`.
      - `def filter(self, record: logging.LogRecord) -> bool`: mutates `record.msg` and `record.args` (be careful with `args` — handle both tuple and dict forms); always returns True.
      - Constants: `MASK_REPLACEMENT = "<REDACTED:{name}>"`.
    - Module-level singleton `_redactor: SecretRedactor` registered on root logger via `install_redactor()` function called once at server startup.
    - `register_token(token: str, name: str = "token") -> None` and `unregister_token(token: str)` helpers.
  - In `src/tiktok_mcp/auth/http_sanitizer.py`:
    - `class SanitizedHttpxError(Exception)`: carries `status: int`, `url: str` (path only, query stripped), `code: int | None` (Business API), `request_id: str | None`, `tiktok_message: str | None`. `__str__` returns ONLY safe context (never the response body).
    - `async def safe_raise_for_status(response: httpx.Response) -> None`: if status >= 400, builds SanitizedHttpxError with body stripped, raises. If status == 200 but Business API code != 0, raises BusinessApiError (delegates to envelopes.py from Task 7 once available — for Task 4 this is a forward declaration; integration test goes in Task 7).
    - `def install_httpx_sanitization(client: httpx.AsyncClient) -> None`: attaches an event hook that calls `safe_raise_for_status` on every response.
  - In `tests/unit/test_redactor.py`:
    - Test: registered token never appears in logged messages (positive: log a message containing the token; assert caplog doesn't contain it).
    - Test: pattern-based redaction catches `Authorization: Bearer abc123` even when token not registered.
    - Test: redaction works on `record.args` dict and tuple forms.
    - Test: SanitizedHttpxError stringifies safely with no body content.
    - Test: `safe_raise_for_status` raises on 401 with the URL path but not the auth header.

  **Must NOT do**:
  - Use regex `.*` patterns that could leak content adjacent to secrets (use word boundary + specific captures)
  - Mutate the original log record string in a way that affects other filters (mutate `msg` AFTER formatting if possible, or use a fresh formatter)
  - Allow `SanitizedHttpxError` to subclass `httpx.HTTPStatusError` (would inherit dangerous `__repr__`)
  - Forget thread safety on the runtime token set (`threading.Lock` or `set` + immutable swap)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Security-critical code; needs careful test coverage of edge cases (token-as-substring of other content, log args dict-form, etc.).
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3, T5, T6, T7, T8)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: ALL Wave 2+ tasks (they use logging + httpx, both must be wrapped)
  - **Blocked By**: 2

  **References**:
  - Python logging filter: `https://docs.python.org/3/library/logging.html#filter-objects`
  - httpx event hooks: `https://www.python-httpx.org/advanced/#event-hooks`
  - Decisions of Record sections 8 (envelope decoders), 12 (redaction is mandatory), 13 (PII rules)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_redactor.py -v` ≥ 6 tests pass
  - [ ] `install_redactor()` is idempotent (calling twice doesn't double-register)
  - [ ] Test demonstrating: log `"got token abc123secret"` after `register_token("abc123secret")` → caplog contains `<REDACTED:token>` and NOT `abc123secret`
  - [ ] Test demonstrating: forced httpx 401 with response body containing `"Bearer xyz789"` → exception string contains URL path but NOT `xyz789` and NOT the full body

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Token registered at runtime never leaks (security-critical)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_redactor.py::test_runtime_token_never_in_caplog -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-4-runtime-redact.txt

  Scenario: Authorization header pattern catches unregistered tokens
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_redactor.py::test_auth_header_pattern -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-4-pattern-redact.txt

  Scenario: httpx 4xx exception body stripped before bubbling
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_redactor.py::test_httpx_exception_stripped -v
    Expected Result: 1 passed; SanitizedHttpxError.__str__ does not contain the body string injected into the mock response
    Evidence: .omo/evidence/task-4-httpx-strip.txt

  Scenario: Idempotent installation
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_redactor.py::test_install_idempotent -v
    Expected Result: 1 passed; root logger has exactly one SecretRedactor filter after install_redactor() called twice
    Evidence: .omo/evidence/task-4-idempotent.txt
  ```

  **Commit**: YES
  - Message: `feat(auth): add SecretRedactor logging filter and httpx exception sanitizer`
  - Files: `src/tiktok_mcp/auth/redactor.py`, `src/tiktok_mcp/auth/http_sanitizer.py`, `tests/unit/test_redactor.py`
  - Pre-commit: pytest + lint + type pass

- [x] 5. **Keychain backend abstraction (keyring primary + cryptography.fernet fallback)**

  **What to do**:
  - In `src/tiktok_mcp/auth/keychain.py`:
    - `class KeychainBackend(Protocol)`: methods `get(key: str) -> str | None`, `set(key: str, value: str) -> None`, `delete(key: str) -> None`, `list_keys(prefix: str) -> list[str]`. All async (httpx is async; keep consistent).
    - `class KeyringBackend(KeychainBackend)`: wraps `keyring` lib. Service name `tiktok-mcp`. Uses `keyring.get_password / set_password / delete_password`. `list_keys` is the hard part — `keyring` itself doesn't enumerate, so we maintain a "keyring index" entry `tiktok-mcp::__index__` that stores a JSON list of known keys; updated atomically on every set/delete.
    - `class EncryptedFileBackend(KeychainBackend)`: stores `tokens.json.enc` under `platformdirs.user_data_dir("tiktok-mcp", appauthor="Signikant", ensure_exists=True)`. Fernet key (32 random bytes base64) is itself stored in OS keyring under key `tiktok-mcp::__fernet_key__`; if keyring is also unavailable, key derived from machine ID + user-prompted passphrase (last-resort; surfaces clear error).
    - `def get_backend() -> KeychainBackend`: tries `KeyringBackend` first; on `keyring.errors.NoKeyringError` falls back to `EncryptedFileBackend`; logs choice once at startup.
    - Key naming convention helpers: `def account_key(api, sandbox, alias) -> str` → `tiktok-mcp::<api>::<sandbox|production>::account::<alias>`; `def app_creds_key(api, sandbox) -> str` → `tiktok-mcp::<api>::<sandbox|production>::app_creds`; `def fernet_key_name() -> str` → `tiktok-mcp::__fernet_key__`.
    - JSON serialization helpers: `Account` + `AccountTokens` combined into one keychain blob `AccountRecord` (private model in this file); read/write atomically.
    - **Windows credential size workaround**: if value > 2KB (Credential Manager limit), use base64 + multi-key chunking with `__partN__` suffix; document in code.
    - **Atomic write** (refresh token rotation): write new under temp suffix `__pending__`, validate readback, then atomic rename (or for keyring: set new under temp key, swap index, delete old).
  - In `tests/unit/test_keychain.py`:
    - Test: KeyringBackend round-trip (set / get / delete / list_keys via index).
    - Test: KeyringBackend chunking for > 2KB values (Windows scenario).
    - Test: EncryptedFileBackend round-trip + decryption requires correct fernet key.
    - Test: `get_backend()` returns KeyringBackend on a system with keyring, falls back on NoKeyringError (monkeypatched).
    - Test: atomic write — interrupt simulation (raise mid-write) leaves keychain in pre-write state, not partially-written.
    - Test: sandbox vs production key namespacing — setting sandbox does NOT leak into production reads.

  **Must NOT do**:
  - Cache decrypted tokens in module-level globals (cache lives only inside `AccountRegistry` from later tasks, with controlled invalidation)
  - Store the fernet key in the same file as the encrypted data (defeats encryption)
  - Use a deterministic fernet key (e.g. derived from hostname only) — would let anyone with the file content decrypt
  - Touch the keychain index from outside `KeyringBackend.set/delete/list_keys` (single point of mutation invariant)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Platform-sensitive code with security implications and atomicity requirements; needs test rigor.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3, T4, T6, T7, T8)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: 10 (account tools), 11 (app cred tools), 12+14 (clients persist token rotation)
  - **Blocked By**: 2, 3 (uses `Account` + `AccountTokens` + `AppCredentials` types)

  **References**:
  - `keyring` lib: `https://pypi.org/project/keyring/` — note backends differ per OS
  - `cryptography.fernet`: `https://cryptography.io/en/latest/fernet/`
  - `platformdirs`: `https://pypi.org/project/platformdirs/` — `user_data_dir(appname, appauthor)` returns OS-correct path
  - Windows Credential Manager 2.5KB limit: Microsoft docs `https://learn.microsoft.com/en-us/windows/win32/api/wincred/`
  - macOS Keychain SecurityAgent dialog UX: document in README that first-launch may prompt
  - Decisions of Record section 5

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_keychain.py -v` ≥ 8 tests pass
  - [ ] On macOS dev machine, `python -c "from tiktok_mcp.auth.keychain import get_backend; b = get_backend(); print(type(b).__name__)"` prints `KeyringBackend`
  - [ ] On Linux without secret-service (CI), same command prints `EncryptedFileBackend`
  - [ ] No raw secret persists in any file outside the encrypted blob (grep test in CI: `grep -rE "(access_token|client_secret|refresh_token).*=.*[A-Za-z0-9]{16,}" .` excluding `tests/cassettes/` and `spikes/` finds nothing)
  - [ ] Sandbox-vs-production isolation test passes (setting sandbox doesn't leak into production reads)

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Round-trip persist + restore + delete (happy path)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_keychain.py::test_keyring_roundtrip -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-5-roundtrip.txt

  Scenario: Atomic refresh — crash mid-write leaves prior token intact
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_keychain.py::test_atomic_write_crash -v
    Expected Result: 1 passed; after monkeypatched-to-crash second-half of write, get(key) returns the ORIGINAL value (not None, not partial)
    Evidence: .omo/evidence/task-5-atomic.txt

  Scenario: Fallback to encrypted file when keyring unavailable
    Tool: Bash (pytest with monkeypatch)
    Steps:
      1. uv run pytest tests/unit/test_keychain.py::test_fallback_to_encrypted_file -v
    Expected Result: 1 passed; `get_backend()` returns EncryptedFileBackend instance
    Evidence: .omo/evidence/task-5-fallback.txt

  Scenario: Sandbox vs production namespacing
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_keychain.py::test_sandbox_production_isolation -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-5-namespace.txt

  Scenario: > 2KB value chunking for Windows
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_keychain.py::test_chunked_large_value -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-5-chunk.txt
  ```

  **Commit**: YES
  - Message: `feat(auth): add keyring + encrypted-file keychain backend with sandbox namespacing`
  - Files: `src/tiktok_mcp/auth/keychain.py`, `tests/unit/test_keychain.py`
  - Pre-commit: pytest + lint + type + grep-no-leaked-secrets pass

- [x] 6. **OAuth state manager (in-memory dict + 10-min TTL + single-use)**

  **What to do**:
  - In `src/tiktok_mcp/auth/state.py`:
    - Module-level `_STATES: dict[str, OAuthInProgress] = {}` guarded by `_LOCK = asyncio.Lock()`.
    - `async def create_state(api_type: ApiType, suggested_alias: str, pkce_verifier: str | None = None) -> OAuthInProgress`: generates 32-byte URL-safe random state token; `expires_at = now() + 10min`; stores in `_STATES`; returns model.
    - `async def consume_state(state: str) -> OAuthInProgress`: under lock, looks up state; if not found → raises `OAuthStateInvalidError(reason="unknown")`; if `expires_at < now()` → raises with `reason="expired"`; otherwise pops from dict (single-use) and returns. Replay detection: if `state` is in a `_RECENTLY_CONSUMED: set[str]` (LRU bounded to 1000, with timestamps), raises `reason="replay"`.
    - `async def cleanup_expired() -> int`: removes states past TTL; returns count cleaned. Optional periodic task in `server.py` runs every 5 min.
    - `async def get_state_count() -> int`: for `get_rate_limit_status` and debugging; returns `len(_STATES)`.
    - `def reset_state_manager() -> None`: pytest helper, clears `_STATES` + `_RECENTLY_CONSUMED`.
  - In `tests/unit/test_state.py`:
    - Test: create → consume returns same OAuthInProgress; second consume raises unknown.
    - Test: create → wait past TTL (use `freezegun` to advance time) → consume raises expired.
    - Test: create → consume → consume again → second raises replay.
    - Test: concurrent create from asyncio.gather of 10 tasks produces 10 distinct states.
    - Test: cleanup_expired removes only expired entries.
    - Test: state token entropy: 100 create_state calls produce 100 unique values; chi-square or simple uniqueness check.

  **Must NOT do**:
  - Use `secrets.token_hex` (lowercase hex only) — use `secrets.token_urlsafe(32)` (more entropy per char, URL-safe)
  - Store state in keychain (overkill, 10-min TTL means in-memory is fine; survives crashes is not required — user retries)
  - Block on the lock for read-only operations (e.g. `get_state_count` doesn't need lock — read is atomic in CPython for dict.len)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Concurrency-sensitive code requiring careful test of edge cases.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3, T4, T5, T7, T8)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: 10 (add_account / complete_account_login use state manager)
  - **Blocked By**: 2, 3 (uses OAuthInProgress + errors)

  **References**:
  - `secrets` stdlib: `https://docs.python.org/3/library/secrets.html`
  - `freezegun` for TTL tests: `https://github.com/spulec/freezegun`
  - Decisions of Record section 11

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_state.py -v` ≥ 6 tests pass
  - [ ] Concurrent stress test: `asyncio.gather` 100 create_state calls returns 100 distinct states
  - [ ] State tokens are URL-safe (regex `^[A-Za-z0-9_-]+$`) and at least 32 bytes of entropy

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Single-use replay protection
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_state.py::test_single_use_replay -v
    Expected Result: 1 passed; second consume of same state raises OAuthStateInvalidError(reason="replay" OR "unknown" depending on cleanup ordering — accept either)
    Evidence: .omo/evidence/task-6-replay.txt

  Scenario: TTL expiry via freezegun
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_state.py::test_ttl_expired -v
    Expected Result: 1 passed; consume after 11 min of frozen time raises reason="expired"
    Evidence: .omo/evidence/task-6-ttl.txt

  Scenario: Concurrent distinct state generation
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_state.py::test_concurrent_distinct -v
    Expected Result: 1 passed; len(set(states)) == 100
    Evidence: .omo/evidence/task-6-concurrent.txt
  ```

  **Commit**: YES
  - Message: `feat(auth): add OAuth state manager with 10-min TTL and single-use replay protection`
  - Files: `src/tiktok_mcp/auth/state.py`, `tests/unit/test_state.py`
  - Pre-commit: pytest + lint + type pass

- [x] 7. **Envelope decoders: BusinessApiResponse + DisplayApiResponse**

  **What to do**:
  - In `src/tiktok_mcp/envelopes.py`:
    - Pydantic model `BusinessApiResponse[T]` (generic): `code: int`, `message: str`, `request_id: str | None`, `data: T | None`.
    - `def decode_business_response(response: httpx.Response, data_model: type[T] | None = None) -> T`:
      1. Verify HTTP status (raise SanitizedHttpxError on >= 400 via Task 4 helper)
      2. Parse JSON body into `BusinessApiResponse`
      3. If `code != 0` → raise `BusinessApiError(code=resp.code, message=resp.message, request_id=resp.request_id, context={...})`
      4. If `data_model` provided, validate `resp.data` against it; else return raw `resp.data`
    - Pydantic model `DisplayApiResponse[T]`: `data: T | None`, `error: DisplayApiErrorPayload | None` where `DisplayApiErrorPayload` has `code: str | None`, `message: str | None`, `log_id: str | None`.
    - `def decode_display_response(response: httpx.Response, data_model: type[T] | None = None) -> T`:
      1. Verify HTTP status
      2. Parse body
      3. If `error.code` is set and is one of the documented error strings (e.g. `"access_token_invalid"`, `"scope_not_authorized"`, `"rate_limit_exceeded"`) → raise typed `DisplayApiError`
      4. Else return validated `data`
    - Both decoders propagate `request_id` / `log_id` into the exception context for support escalation.
    - Add error-code-to-typed-exception mapping tables for both APIs (extend over time as we encounter more codes; start with the well-known ones from research findings).
  - In `tests/unit/test_envelopes.py`:
    - Test: BusinessApiResponse with `code=0` returns data.
    - Test: BusinessApiResponse with `code=40000` raises BusinessApiError with correct `code`, `message`, `request_id`.
    - Test: replay cassette from spike S3 (or fresh fixture mimicking it) — assert decoder raises BusinessApiError.
    - Test: BusinessApiResponse missing `code` field — defensive default; raises with `code=-1` and a "malformed response" message.
    - Test: DisplayApiResponse with `data` returns data.
    - Test: DisplayApiResponse with `error.code="access_token_invalid"` raises DisplayApiError with the same code.
    - Test: BusinessApiResponse with explicit `data_model=AdvertiserInfo` validates the inner data against the model (round-trip).
    - Test: HTTP 401 on either API raises SanitizedHttpxError (status preserved, body stripped).

  **Must NOT do**:
  - Bury the original response body in the exception (defeats redaction)
  - Use `response.raise_for_status()` directly (it dumps body to exception); use Task-4 sanitizer
  - Skip `request_id` propagation (it's the support escalation path)
  - Subclass `BusinessApiError` from `httpx.HTTPError` (would cause double-handling)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Cross-cutting concern affecting every API call; must be airtight.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3, T4, T5, T6, T8)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: 12 (Display client), 14 (Business client), and through them ALL Wave 2 read tools and Wave 3 write tools
  - **Blocked By**: 2, 3, 4 (uses types + http sanitizer)

  **References**:
  - Spike S3 cassette (`spikes/cassettes/s3_business_error.yaml`) — canonical example of `code != 0` response
  - Decisions of Record section 8
  - Display API error reference: research findings (look for `error.code` table in Display API ref card)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_envelopes.py -v` ≥ 7 tests pass
  - [ ] BusinessApiError raised on `code != 0` is the SAME error type as raised by spike S3 (verify import path equality)
  - [ ] `decode_business_response(resp, data_model=AdvertiserInfo)` validates a known-good fixture without error
  - [ ] No httpx response body content appears in any test failure trace (capture pytest output with the deliberate-failure test; grep for known body content)

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Business code=0 happy path returns data
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_envelopes.py::test_business_success -v
    Expected Result: 1 passed
    Evidence: .omo/evidence/task-7-business-ok.txt

  Scenario: Business code != 0 raises BusinessApiError with request_id preserved
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_envelopes.py::test_business_error_with_request_id -v
    Expected Result: 1 passed; exception has code, message, request_id attrs
    Evidence: .omo/evidence/task-7-business-err.txt

  Scenario: Spike S3 cassette is compatible with production decoder
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_envelopes.py::test_production_decoder_replays_spike_s3 -v
    Expected Result: 1 passed (decoder raises same exception type as spike prototype)
    Evidence: .omo/evidence/task-7-spike-compat.txt

  Scenario: Display error codes map to typed exceptions
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_envelopes.py::test_display_error_codes -v
    Expected Result: 1 passed; tests for ≥ 3 known error codes
    Evidence: .omo/evidence/task-7-display-err.txt

  Scenario: Body never leaks into exception
    Tool: Bash (pytest + grep)
    Steps:
      1. uv run pytest tests/unit/test_envelopes.py::test_body_redacted_in_exception -v 2>&1 | grep -E "secret_body_content_marker"
    Expected Result: grep exit code 1 (no match)
    Evidence: .omo/evidence/task-7-no-body-leak.txt
  ```

  **Commit**: YES
  - Message: `feat(envelopes): add BusinessApiResponse + DisplayApiResponse decoders with typed errors`
  - Files: `src/tiktok_mcp/envelopes.py`, `tests/unit/test_envelopes.py`
  - Pre-commit: pytest + lint + type pass; spike-compat test pass

- [x] 8. **`require_writes_enabled` decorator + env-var parser**

  **What to do**:
  - In `src/tiktok_mcp/decorators.py`:
    - `def parse_writes_env(value: str | None) -> set[str]`: returns the set of enabled api_types from the env var. Cases:
      - None / `""` / `"0"` / `"false"` / `"False"` / `"no"` → `set()` (none enabled)
      - `"1"` / `"true"` / `"True"` / `"yes"` / `"all"` → `{"display", "marketing", "comments", "posting"}` (all)
      - comma-separated list (e.g. `"marketing,comments"`) → set of trimmed lowercase tokens; unknown tokens logged at WARNING but ignored
    - `def writes_enabled_for(api: str, env_value: str | None = None) -> bool`: reads `os.environ["TIKTOK_MCP_ALLOW_WRITES"]` if `env_value` not provided; returns `api in parse_writes_env(...)`. Always reads at call time (not cached) so toggling works mid-session.
    - `def require_writes_enabled(api: str) -> Callable`: decorator factory; wraps an async function. Behavior on invocation: if NOT enabled → returns the structured error dict (Decisions section 4); if enabled → calls the wrapped function. Decorator name set on `__wrapped__` so introspection works.
    - Tool-metadata helper `def is_destructive(fn) -> bool`: returns True if `fn` has the decorator marker attribute `__tiktok_mcp_destructive__ = True` (set by the decorator).
    - Compile-time enforcement (lint check): every function under `src/tiktok_mcp/tools/` whose name matches one of the write/destructive patterns (`create_*`, `update_*`, `delete_*`, `pause_*`, `resume_*`, `upload_*`, `post_*`, `pin_*`, `unpin_*`, `hide_*`, `unhide_*`, `remove_*`, `set_*`, `add_*`, `complete_*`, `rename_*`, `publish_*`, `move_*`, `finalize_*`) MUST have either `__tiktok_mcp_destructive__` or `__tiktok_mcp_read_only__` attribute. A custom pytest test traverses the tools module at runtime and asserts this — catches missing decorators in CI.
  - In `tests/unit/test_decorators.py`:
    - Test: `parse_writes_env` exhaustive truth table (all the env-var values).
    - Test: decorator blocks call when env unset; returns `writes_disabled` error dict.
    - Test: decorator allows call when env=`all`; passes through to wrapped fn.
    - Test: decorator allows call when env=`marketing` AND `api="marketing"`; blocks when `api="comments"`.
    - Test: env-var changes mid-session take effect (set `all`, call → ok; clear env, call → blocked).
    - Test: structured error contains all required fields (`error`, `message`, `tool`, `api`, `would_have_done`).
    - Test (compile-time enforcement): metaprogramming check across `src/tiktok_mcp/tools/` — runs at test time, catches an intentionally undecorated write-named function in a fixture as a positive case.

  **Must NOT do**:
  - Cache the env var read at module import (must re-read on every call)
  - Use a single global `WRITES_ENABLED = True/False` flag (loses per-API granularity)
  - Raise an exception on blocked calls (must return structured dict; exceptions confuse MCP protocol semantics)
  - Print to stdout when blocked (logs go to stderr; the structured dict goes back to the MCP client)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Safety-critical decorator with subtle correctness requirements (env re-read per call; per-API granularity; compile-time enforcement).
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3, T4, T5, T6, T7)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: ALL Wave 3 write tools (T20-T28) and write-tool setup tools in Wave 2 (T10, T11)
  - **Blocked By**: 2, 3 (uses errors module)

  **References**:
  - Decisions of Record section 4 (TIKTOK_MCP_ALLOW_WRITES specification)
  - Metis-mandated acceptance criterion 6.1 (parametrized env-var test)
  - Python `functools.wraps`: `https://docs.python.org/3/library/functools.html#functools.wraps`

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_decorators.py -v` ≥ 8 tests pass
  - [ ] Parametrized truth table covers all 8 env-var values listed in Decisions section 4 table
  - [ ] Compile-time enforcement test catches an intentionally undecorated write fixture function
  - [ ] `writes_enabled_for("marketing", env_value="marketing,comments")` returns True
  - [ ] `writes_enabled_for("posting", env_value="marketing,comments")` returns False

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Truth-table parametrized test (Metis-mandated 6.1)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_decorators.py::test_writes_env_truth_table -v
    Expected Result: ≥ 8 parametrized cases all pass (covering unset, "", "0", "false", "1", "true", "all", "marketing,comments")
    Evidence: .omo/evidence/task-8-truth-table.txt

  Scenario: Env-var toggle mid-session takes effect (no caching)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_decorators.py::test_env_toggle_takes_effect -v
    Expected Result: 1 passed; same decorated fn returns ok then writes_disabled after env cleared
    Evidence: .omo/evidence/task-8-toggle.txt

  Scenario: Structured error response on block
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_decorators.py::test_block_returns_structured_error -v
    Expected Result: 1 passed; result dict contains keys {error, message, tool, api, would_have_done}; error=="writes_disabled"
    Evidence: .omo/evidence/task-8-structured.txt

  Scenario: Compile-time enforcement (lint-like)
    Tool: Bash (pytest)
    Steps:
      1. uv run pytest tests/unit/test_decorators.py::test_enforcement_catches_missing_decorator -v
    Expected Result: 1 passed; the fixture undecorated_write_fn() triggers AssertionError from the enforcement scan
    Evidence: .omo/evidence/task-8-enforcement.txt
  ```

  **Commit**: YES
  - Message: `feat(decorators): add require_writes_enabled with per-API granularity and runtime enforcement`
  - Files: `src/tiktok_mcp/decorators.py`, `tests/unit/test_decorators.py`
  - Pre-commit: pytest + lint + type pass

- [x] 9. **API Surface Inventory (`docs/api-surface-inventory.md`) — gating deliverable**

  **What to do**:
  - Produce `docs/api-surface-inventory.md` enumerating EVERY endpoint shipping in v0.1 across the 4 API surfaces. This is the canonical reference Wave 2 + Wave 3 tools are built from. "Full featureset" is uncomputable without it.
  - Format: one section per API surface, then one row per endpoint, in a markdown table with columns:
    - **MCP tool name** (the function name in `src/tiktok_mcp/tools/`)
    - **TikTok endpoint path** (exact path)
    - **HTTP method**
    - **Required scope** (OAuth scope name)
    - **Tool annotation** (`readOnlyHint` or `destructiveHint`)
    - **`TIKTOK_MCP_ALLOW_WRITES` namespace** (`display` / `marketing` / `comments` / `posting`, or `—` for reads)
    - **Wave** (2 or 3)
    - **Implementation task ID** (forward reference to T13/T15/T17/T18 etc.)
  - Source authority: the explore agent's structured extraction (bg_32690cb8) + focused comments research (bg_066e9675). If those are not yet available at execution time, the implementing agent MUST re-research the endpoints via `librarian` agent calls before writing the inventory. NO guessing.
  - Open the document with: a one-paragraph "How to read this" + the explicit list of EXCLUDED endpoints / surfaces per Decisions of Record section 2 (Catalog Manager, Audience Segments, Reservation, Pixel/Events, DPA, Research API, comment search, slideshow, scraping, etc.). Each excluded item gets a one-liner reason + "deferred to v0.2".
  - Close the document with a "Tool count by surface" rollup and an open-issues section for any endpoint where the implementing agent could not confidently determine the canonical 2026 path (these become spike-style sub-tasks before Wave 2 begins).
  - **Expected approximate counts** (final numbers determined by inventory):
    - Display API: ~5-8 tools (user info, video list, video query, oauth flows, possible writes)
    - Marketing API: ~25-35 tools (advertiser/BC, campaign/adgroup/ad CRUD × 5 ops, reports sync + async + poll + download, audience/creative uploads, pause/resume etc.)
    - Business Organic (comments): ~6-9 tools (list comments, list replies, post reply, pin/unpin, hide/unhide, delete)
    - Content Posting: ~8-12 tools (init upload, upload chunk, finalize, pull-from-URL, photo upload, status polling, draft list/publish/delete/move)
    - Setup / accounts / utility: ~10 tools (set/list/verify app creds, add/list/rename/remove account, complete_login, get_rate_limit_status, resources)
    - **Total target**: 60-80 MCP tools

  **Must NOT do**:
  - Guess endpoints based on naming conventions ("there should be a `/comment/get/`") — if not in research, mark as OPEN and don't include
  - Include any v0.2-deferred surface in the v0.1 row set
  - Include scraping or no-auth endpoints
  - Forget the `destructiveHint` annotation column (compile-time check from T8 enforces it; inventory documents it)

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: Documentation-heavy task; clarity and exhaustiveness are the deliverable.
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES (with T3-T8 — they don't depend on inventory; inventory is the human-readable contract Wave 2/3 honors)
  - **Parallel Group**: Wave 1 main
  - **Blocks**: ALL Wave 2 and Wave 3 task expansion. The inventory MUST be in place before Wave 2 begins so Sisyphus's per-tool task spec is concrete.
  - **Blocked By**: 2 (uses repo layout); references research findings from bg_32690cb8 + bg_066e9675

  **References**:
  - Research outputs:
    - `/Users/user/.local/share/opencode/tool-output/tool_e4ef180a3001JdkDjDNOact5GN` (Display API endpoints + Content Posting)
    - bg_32690cb8 explore-agent structured extract (when available)
    - bg_066e9675 comments-focused librarian output (when available)
  - Authoritative TikTok docs (re-fetch fresh if research findings stale):
    - `https://developers.tiktok.com/doc/` — Display + Content Posting + Login Kit
    - `https://business-api.tiktok.com/portal/docs` — Marketing + Business Organic
  - Decisions of Record sections 1 (4 surfaces), 2 (IN/OUT lists), 4 (per-API write namespaces)

  **Acceptance Criteria**:
  - [ ] `docs/api-surface-inventory.md` exists
  - [ ] Contains ≥ 4 surface sections (Display, Marketing, Business Organic, Content Posting) plus Setup/Utility section
  - [ ] Total tool rows ≥ 50 (lower bound; full featureset target 60-80)
  - [ ] Every row has a populated annotation column (no blank `readOnlyHint`/`destructiveHint`)
  - [ ] Every destructive row has a populated `TIKTOK_MCP_ALLOW_WRITES` namespace column
  - [ ] EXCLUDED list explicitly covers every Decisions section-2 OUT item
  - [ ] `grep -c "destructiveHint" docs/api-surface-inventory.md` ≥ 20 (matches the writes-heavy scope)
  - [ ] No "TBD" or empty cells in the body of the inventory tables (open issues live in a dedicated section, not as ambiguous rows)
  - [ ] Tool count rollup at the bottom adds to a total within the 60-80 target band

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Inventory file structure validates against template
    Tool: Bash (python script)
    Steps:
      1. python -c "import re; t = open('docs/api-surface-inventory.md').read(); assert all(s in t for s in ['## Display API', '## Marketing API', '## Business Organic', '## Content Posting', '## Setup', '## Excluded', '## Tool count'])"
    Expected Result: assertion passes (exit 0)
    Evidence: .omo/evidence/task-9-structure.txt

  Scenario: Every table row has required columns populated
    Tool: Bash (python script)
    Steps:
      1. Run an inline Python script that parses each markdown table row and asserts no column is empty (excluding the optional notes column)
    Expected Result: exit 0; every row passes
    Evidence: .omo/evidence/task-9-rows-complete.txt

  Scenario: Total tool count within target band
    Tool: Bash (python)
    Steps:
      1. python -c "import re; t = open('docs/api-surface-inventory.md').read(); rows = sum(1 for line in t.split('\\n') if re.match(r'^\\| [a-z_]+ \\|', line)); print(rows); assert 50 <= rows <= 100"
    Expected Result: exit 0; printed count between 50-100
    Evidence: .omo/evidence/task-9-count.txt

  Scenario: Every Decisions-OUT item appears in EXCLUDED section
    Tool: Bash (grep)
    Steps:
      1. for item in "Catalog Manager" "Audience Segments" "Reservation" "Pixel" "DPA" "Research API" "comment search" "slideshow"; do grep -q "$item" docs/api-surface-inventory.md || (echo "MISSING: $item"; exit 1); done
    Expected Result: all greps succeed; exit 0
    Evidence: .omo/evidence/task-9-excludes.txt
  ```

  **Commit**: YES
  - Message: `docs(inventory): enumerate v0.1 API surface across Display, Marketing, Business Organic, Content Posting`
  - Files: `docs/api-surface-inventory.md`
  - Pre-commit: all four QA scenario scripts pass

### Wave 2 — Auth tools + read tools across 4 APIs

> Format note: Wave 2/3 task specs are tighter. Same rigour, less verbosity per task. The shared patterns (FastMCP `@app.tool()` registration, `Context` parameter, structured error returns, `BusinessApiResponse` decoder use, etc.) are defined once in Decisions of Record + Wave 1 modules; tasks reference those by name.

- [x] 10. **Account management MCP tools (add / complete_login / list / rename / remove)**

  **What to do**: Implement 5 MCP tools in `src/tiktok_mcp/tools/accounts.py`:
  1. `add_account(api_type: ApiType, alias: str | None = None) -> dict` — destructiveHint. Creates OAuth state via T6, builds the per-api authorization URL (Display: `https://www.tiktok.com/v2/auth/authorize/` with `code_challenge` for PKCE; Business: `https://business-api.tiktok.com/portal/auth?app_id=&redirect_uri=&state=`; Content Posting: same as Display + scopes `video.upload`,`video.publish`), suggests `<country>-<type>-<short_id>` alias, returns `{ url, state, suggested_alias, expires_in: 600, instructions }`.
  2. `complete_account_login(redirect_url: str, alias_override: str | None = None) -> dict` — destructiveHint. Parses URL (handles markdown/whitespace per Metis), consumes state, validates host against registered redirect on `AppCredentials`, exchanges code for tokens (POST to token endpoint per api_type), atomically persists via T5 keychain backend, registers token in T4 redactor, returns `AccountSummary`.
  3. `list_accounts() -> list[AccountSummary]` — readOnlyHint. Enumerates keychain via T5 `list_keys`, returns sanitized summaries (no tokens, no raw tiktok_id).
  4. `rename_account(old_alias: str, new_alias: str) -> AccountSummary` — destructiveHint. Validates new alias format + uniqueness; atomic rename in keychain (write new key, delete old).
  5. `remove_account(alias: str, confirmation_token: str | None = None) -> dict` — destructiveHint. **Two-step**: first call returns `{ pending_removal: true, confirmation_token, expires_in: 60 }`; second call with matching token unregisters tokens from redactor + deletes keychain entries.

  All five gated by appropriate decorators: setup tools count as `display`/`marketing`/`comments`/`posting` depending on `api_type` for granular `TIKTOK_MCP_ALLOW_WRITES` (e.g. adding a Marketing account needs `marketing` enabled). But: there's a UX trap — gating account ADDS feels heavy-handed. **Decision**: account-management tools gate on a SEPARATE env: `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` (default off; same parser as writes). Document in Decisions of Record addendum at top of file. Justify: account add/remove is intrinsically destructive but adjacent to setup, not a "real" data-mutating write; users want one knob for "let Claude add accounts during onboarding" vs another for "let Claude post comments".

  Add 6 confirmation-token entries to in-memory `_PENDING_REMOVALS: dict[alias, (token, expires_at)]` with 60s TTL; cleanup loop or lazy expiry.

  **Must NOT do**: log full redirect URL (contains `code`); echo tokens in return dicts; let `list_accounts` return any `SecretStr` field; allow removal of an alias still being used by an in-flight tool call (best-effort: check no in-flight calls hold an `AccountTokens` cache entry; if so, return `account_busy` error).

  **Recommended Agent**: `unspecified-high` (multi-tool implementation with tricky two-step semantics). Skills: none.

  **Parallelization**: Can parallel with T11, T12, T14, T17, T18. Blocks T13/T15-T19/T20-T28. Blocked by T3, T5, T6, T7, T8 + S1.

  **References**:
  - Spike S1 results (`spikes/s1_results.md`) — confirmed auth URL constructions per api_type
  - bg_32690cb8 + bg_066e9675 explore extracts for exact endpoint hosts + scopes
  - Decisions of Record sections 3 (auth flow), 4 (writes gating), 14 (account model)
  - Metis acceptance criteria 6.7, 6.8, 6.9, 6.10, 6.12, 6.17

  **Acceptance Criteria**:
  - [ ] All 5 tools registered as MCP tools (visible in `tools/list`)
  - [ ] `pytest tests/integration/test_accounts.py` covers happy paths + each Metis 6.7-6.12, 6.17 scenario with vcrpy cassettes
  - [ ] Manual MCP boot test: spawn server, call `tools/list`, see all 5; call `add_account(api_type="display")`, get URL; call `complete_account_login(redirect_url=<from S1>)`, get AccountSummary
  - [ ] `list_accounts` after add shows the new alias; `remove_account` two-step purges it
  - [ ] Concurrent two-flow test passes (Metis 6.8)
  - [ ] Duplicate alias rejection returns `alias_taken` with `suggestion` (Metis 6.9)

  **QA Scenarios**:

  ```
  Scenario: End-to-end add + complete + list happy path (using vcr cassette)
    Tool: pytest with vcrpy
    Steps: pytest tests/integration/test_accounts.py::test_add_complete_list_happy
    Expected: vcr cassette replays OAuth → AccountSummary returned with sandbox=true; list_accounts shows alias
    Evidence: .omo/evidence/task-10-happy.txt

  Scenario: Markdown-paste URL robustness (Metis 6.12)
    Tool: pytest parametrized
    Steps: pytest tests/integration/test_accounts.py::test_paste_robustness -v
    Expected: 5 parametrized inputs (whitespace, backticks, markdown link, newlines, double-encoded) all parsed successfully
    Evidence: .omo/evidence/task-10-paste.txt

  Scenario: remove_account requires confirmation_token (Metis 6.17)
    Tool: pytest
    Steps: pytest tests/integration/test_accounts.py::test_remove_two_step -v
    Expected: first call returns pending_removal=true, second call with token returns removed=true, second call WITHOUT token returns pending_removal again
    Evidence: .omo/evidence/task-10-twostep.txt

  Scenario: Live MCP boot — tools/list response shape
    Tool: interactive_bash (spawn mcp-cli or use mcp inspector)
    Steps:
      1. timeout 5s uv run tiktok-mcp <<< '{"jsonrpc":"2.0","method":"tools/list","id":1}'
    Expected: stdout JSON contains "add_account", "complete_account_login", "list_accounts", "rename_account", "remove_account"
    Evidence: .omo/evidence/task-10-mcp-list.txt
  ```

  **Commit**: `feat(accounts): add multi-account OAuth setup tools (add/complete/list/rename/remove with two-step)` — `src/tiktok_mcp/tools/accounts.py`, `tests/integration/test_accounts.py`, `tests/cassettes/oauth_*.yaml`

- [x] 11. **App credentials MCP tools (set / list / verify)**

  **What to do**: Implement 3 MCP tools in `src/tiktok_mcp/tools/app_credentials.py`:
  1. `set_app_credentials(api_type: ApiType, client_id: str, client_secret: str, sandbox: bool = False) -> AppCredentialsSummary` — destructiveHint. Validates via type schema; writes via T5 keychain under `tiktok-mcp::<api>::<mode>::app_creds`; registers `client_secret` in T4 redactor; returns summary (fingerprint, NEVER raw). No `verified` tagging — verification is ephemeral via T3's `AppCredentialsVerifyResult` model returned by `verify_app_credentials`.
  2. `list_app_credentials() -> list[AppCredentialsSummary]` — readOnlyHint. Enumerates app-creds keychain entries; returns summaries (fingerprints only).
  3. `verify_app_credentials(api_type: ApiType, sandbox: bool = False) -> AppCredentialsVerifyResult` — **`readOnlyHint`** (truly read-only: performs a no-cost API call e.g. Display API `/v2/oauth/token/` with `grant_type=client_credentials` if supported, or a token introspection). Returns the dedicated `AppCredentialsVerifyResult` model (defined in T3): `valid: bool`, `verified_at: datetime` (timestamp of THIS call — ephemeral), `error_code/error_message` on failure. Does NOT mutate keychain. The verification result is NOT persisted; callers who want a persistent "last verified" must record the result themselves out-of-band. This keeps the tool semantically read-only and avoids the readOnly-but-mutates conflict flagged by Oracle phase-2 review.

  Tools 1 (`set_app_credentials`) is gated by `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` (same env as T10). Tools 2 (`list_app_credentials`) and 3 (`verify_app_credentials`) are read-only and NOT gated — they only inspect keychain state without mutation.

  **Must NOT do**: return raw `client_secret` from any tool (compile-time-checkable: grep test `grep -r 'client_secret' src/tiktok_mcp/tools/` returns no raw exports); log client_id with > 4 chars visible; cache credentials in module globals (always read from keychain on demand).

  **Recommended Agent**: `unspecified-high`. Skills: none.

  **Parallelization**: Can parallel with T10, T12-T19. Blocks T12/T14 (clients need creds to make calls). Blocked by T3, T5, T8.

  **References**: Decisions of Record sections 5 (storage), addendum on `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES`; Metis review on "list_app_credentials must NEVER return secrets".

  **Acceptance Criteria**:
  - [ ] 3 tools registered
  - [ ] `pytest tests/integration/test_app_credentials.py` covers set/list/verify happy + secret-leak negative test
  - [ ] `grep -r "client_secret" src/tiktok_mcp/tools/app_credentials.py | grep -vE "(SecretStr|annotation|comment|docstring)" | wc -l` == 0
  - [ ] Calling `list_app_credentials` after `set_app_credentials` returns summary with `client_secret_set=true` and NO secret value in response

  **QA Scenarios**:

  ```
  Scenario: set + list + verify happy path
    Tool: pytest
    Steps: pytest tests/integration/test_app_credentials.py::test_set_list_verify -v
    Expected: pass; (a) `set_app_credentials(...)` returns `AppCredentialsSummary` with `client_secret_set=true`, NO secret value; (b) `list_app_credentials()` returns the new entry with same `client_secret_set=true` and NO `verified` or `last_verified_at` fields (those were removed in T3 per Oracle phase-2 review); (c) `verify_app_credentials(...)` returns the dedicated `AppCredentialsVerifyResult` model with `valid=true`, `verified_at=<recent datetime>`, `error_code=None`; (d) calling `list_app_credentials()` AGAIN after verify returns the SAME shape as in (b) — verify did NOT mutate any persisted state; (e) secret never appears in any tool's response across (a)/(b)/(c)/(d).
    Evidence: .omo/evidence/task-11-happy.txt

  Scenario: Secret never leaks (security check)
    Tool: pytest
    Steps: pytest tests/integration/test_app_credentials.py::test_secret_never_returned -v
    Expected: pass; result.model_dump_json() asserts secret NOT in output for set/list/verify
    Evidence: .omo/evidence/task-11-no-leak.txt

  Scenario: Sandbox vs production isolation (Metis 6.15)
    Tool: pytest
    Steps: pytest tests/integration/test_app_credentials.py::test_sandbox_isolation -v
    Expected: setting sandbox=true creds; list_app_credentials with sandbox=false returns empty; setting sandbox=false creds shows up separately
    Evidence: .omo/evidence/task-11-isolation.txt
  ```

  **Commit**: `feat(app_credentials): add set/list/verify_app_credentials tools with fingerprint-only returns` — `src/tiktok_mcp/tools/app_credentials.py`, `tests/integration/test_app_credentials.py`

- [x] 12. **Display API client (httpx + auth + retry + rate limit + envelope)**

  **What to do**: In `src/tiktok_mcp/api/display/client.py`:
  - `class DisplayAPIClient`: constructor accepts `Account` + `AppCredentials`; lazily creates `httpx.AsyncClient` with sane defaults (`timeout=30s`, base_url=`https://open.tiktokapis.com`, `event_hooks=[install_httpx_sanitization]`).
  - `async def _ensure_fresh_token(self) -> str`: per-account `asyncio.Lock` (T6 pattern but on Account, not state). Checks `access_token_expires_at`; if < 5min from now, refreshes via POST `https://open.tiktokapis.com/v2/oauth/token/` with `grant_type=refresh_token`, persists new tokens atomically via T5, updates redactor.
  - `async def request(method, path, *, params=None, json=None) -> Any`: composes Authorization header, sets `Content-Type: application/json` for POST, calls httpx, runs `decode_display_response` (T7), wraps in `tenacity` retry (max 3, `Retry-After` aware) for 429 + 5xx idempotent.
  - Token revocation handling: on `DisplayApiError(code="access_token_invalid")` mid-call, attempt ONE refresh+retry; on second 401, mark `account.status=BROKEN` via keychain update, raise `AccountBrokenError`.
  - Refresh-token-rotation: when token endpoint returns new `refresh_token`, write new BEFORE clearing old; if write fails, log + raise; in-memory cache updates atomically under lock.

  **Must NOT do**: cache decoded response bodies in client (separate concern); share a single httpx client across accounts (one per Account instance; tokens differ); use `requests` lib (httpx async only); silently catch `tenacity.RetryError` (let propagate).

  **Recommended Agent**: `unspecified-high`. Skills: none.

  **Parallelization**: Can parallel with T11, T14, T17, T18. Blocks T13 (Display read tools). Blocked by T3, T4, T5, T7.

  **References**: bg_32690cb8 extract (Display API endpoint paths + token refresh semantics + rate limit shape); `tenacity` docs `https://tenacity.readthedocs.io/`; Decisions of Record sections 6 (concurrency), 7 (rate limit), 8 (envelopes).

  **Acceptance Criteria**:
  - [ ] `pytest tests/integration/test_display_client.py` covers: token-refresh-on-expiry (Metis 6.3), refresh-token-rotation atomicity (Metis 6.4), rate-limit Retry-After respect (Metis 6.5), 401-retry-once-then-broken
  - [ ] Per-account lock test: two parallel calls on same account both refresh exactly once (not twice)
  - [ ] No httpx response body leaks into exception strings (grep test on pytest output)

  **QA Scenarios**:

  ```
  Scenario: Token refresh on expiry (Metis 6.3)
    Tool: pytest with vcr + freezegun
    Steps: pytest tests/integration/test_display_client.py::test_auto_refresh -v
    Expected: pass; cassette: 1st call returns 401 invalid_token, 2nd call (refresh) succeeds, 3rd call (original retry) succeeds; client.request returns valid data
    Evidence: .omo/evidence/task-12-refresh.txt

  Scenario: Refresh-token rotation atomicity (Metis 6.4)
    Tool: pytest
    Steps: pytest tests/integration/test_display_client.py::test_rt_rotation -v
    Expected: after refresh returning new RT, keychain has new value AND old RT is no longer present (atomic swap); test verifies no window where both exist
    Evidence: .omo/evidence/task-12-rt-rotate.txt

  Scenario: 429 Retry-After respected (Metis 6.5)
    Tool: pytest with vcr
    Steps: pytest tests/integration/test_display_client.py::test_retry_after -v
    Expected: cassette: 429 with Retry-After: 2 → 200; total elapsed time 1.8s ≤ t ≤ 3.0s
    Evidence: .omo/evidence/task-12-retry-after.txt

  Scenario: Concurrent refresh deduplication
    Tool: pytest with asyncio.gather
    Steps: pytest tests/integration/test_display_client.py::test_concurrent_refresh_dedupe -v
    Expected: gather of 5 parallel requests on same account when token expired triggers exactly 1 refresh POST (assert via httpx-inspector call counter)
    Evidence: .omo/evidence/task-12-concurrent.txt
  ```

  **Commit**: `feat(api/display): add DisplayAPIClient with token refresh, rate limit retry, atomic RT rotation` — `src/tiktok_mcp/api/display/client.py`, `tests/integration/test_display_client.py`, `tests/cassettes/display_*.yaml`

- [x] 13. **Display API read tools (user info, video list, video query, video metrics, oauth utilities)**

  **What to do**: In `src/tiktok_mcp/tools/display_read.py`, register MCP tools (all readOnlyHint) calling T12 client:
  1. `display_get_user_info(alias: str, fields: list[str] | None = None) -> UserInfo` — POST `/v2/user/info/` body. Default field set per Decisions; accepts override list. Returns pydantic-validated UserInfo (open_id, union_id, avatar_url, display_name, bio_description, follower_count, following_count, likes_count, video_count, is_verified, profile_deep_link — subset gated by scope).
  2. `display_list_videos(alias: str, cursor: int | None = None, max_count: int = 20, fields: list[str] | None = None) -> dict` — POST `/v2/video/list/`. Returns `{ videos: list[Video], cursor: int, has_more: bool }`.
  3. `display_query_videos(alias: str, video_ids: list[str], fields: list[str] | None = None) -> list[Video]` — POST `/v2/video/query/` with `{ filters: { video_ids: [...] } }`. Up to 20 ids per call; validate.
  4. `display_get_video_metrics(alias: str, video_id: str) -> VideoMetrics` — convenience wrapper around query_videos for the metrics-relevant field set (view_count, like_count, comment_count, share_count, embed_html, embed_link, etc.).
  5. `display_refresh_token(alias: str) -> dict` — destructiveHint (mutates keychain). **Decorated `@require_writes_enabled("display")` per DoR §4** (token rotation is destructive). Forces a refresh and returns the new expiry; useful for "you're about to do something long, refresh first".
  6. `display_revoke_token(alias: str) -> dict` — destructiveHint. **Decorated `@require_writes_enabled("display")` per DoR §4** (revokes TikTok-side authorization). POST `/v2/oauth/revoke/`; mark account.status=REVOKED but DON'T delete; user can `add_account` again later or `remove_account` to purge.

  Pydantic models: `UserInfo`, `Video`, `VideoMetrics` in `src/tiktok_mcp/api/display/models.py`. All Pydantic v2; align with T3 conventions.

  **Must NOT do**: include `cover_image_url` in cacheable response without TTL warning (6h); auto-refresh `cover_image_url` (let caller re-query); expose `union_id` if scope didn't grant it (per-field scope-gating).

  **Recommended Agent**: `unspecified-high`. Skills: none.

  **Parallelization**: Can parallel with T15, T17, T18, T19. Blocks none (read tools are leaves). Blocked by T12.

  **References**: bg_32690cb8 extract → Display API fields-per-scope table; Decisions of Record section 2 (Display IN list).

  **Acceptance Criteria**:
  - [ ] 6 tools registered (5 reads + 2 token-utility — actually 4 reads + 2 dest = 6 total)
  - [ ] `pytest tests/integration/test_display_read.py` covers each tool with vcrpy cassettes
  - [ ] Multi-account isolation test (Metis 6.2): two accounts; `display_get_user_info(alias="a")` uses TOKEN_A in Authorization header; alias="b" uses TOKEN_B (httpx-inspector assertion)

  **QA Scenarios**:

  ```
  Scenario: Each read tool replays cassette + parses response
    Tool: pytest
    Steps: pytest tests/integration/test_display_read.py -v
    Expected: ≥ 6 tests pass
    Evidence: .omo/evidence/task-13-reads.txt

  Scenario: Multi-account isolation (Metis 6.2)
    Tool: pytest with httpx-inspector
    Steps: pytest tests/integration/test_display_read.py::test_isolation -v
    Expected: assert Authorization header per-call matches account token
    Evidence: .omo/evidence/task-13-isolation.txt

  Scenario: Pagination respects native cursor passthrough (Decisions section 9)
    Tool: pytest
    Steps: pytest tests/integration/test_display_read.py::test_pagination_passthrough -v
    Expected: response shape has `cursor` and `has_more` fields preserved from upstream
    Evidence: .omo/evidence/task-13-pagination.txt
  ```

  **Commit**: `feat(tools/display): add user_info + video_list/query/metrics + token utilities` — `src/tiktok_mcp/tools/display_read.py`, `src/tiktok_mcp/api/display/models.py`, `tests/integration/test_display_read.py`, `tests/cassettes/display_*.yaml`

- [x] 14. **Business API client (httpx + auth + BusinessApiResponse + retry)**

  **What to do**: In `src/tiktok_mcp/api/business/client.py`:
  - `class BusinessAPIClient`: mirrors T12 DisplayAPIClient structure but specialized for Business API.
  - Base URL `https://business-api.tiktok.com`. Auth header style: Business API uses **`Access-Token: <token>` header** (NOT Bearer). Document this in code comments — it's a key divergence from Display.
  - Refresh: Business API token refresh is more limited; some tokens never refresh (long-lived). On `code=40100`/`40105` (token expired / invalid), surface `AccountBrokenError` immediately (re-auth required) rather than attempting refresh — unless a refresh_token is present in the AccountTokens record (Business API recently added long-lived refresh in some endpoints; check current docs).
  - All responses go through `decode_business_response` (T7) — Business API uses HTTP 200 + `code != 0` envelope.
  - Retry policy: same as Display (tenacity, 429, Retry-After, max 3).
  - Per-account asyncio.Lock on refresh path (T6 pattern).

  **Must NOT do**: use `Bearer` prefix (wrong for Business API); attempt to silently refresh when refresh_token absent (surface clearly); assume HTTP 200 = success (always decode envelope first).

  **Recommended Agent**: `unspecified-high`.

  **Parallelization**: Parallel with T10-T13, T17-T19. Blocks T15, T16, T17, T18, T20-T28. Blocked by T3, T4, T5, T7.

  **References**: bg_32690cb8 + bg_f4ba0e78 thinking transcripts → Business API auth header is `Access-Token` (confirmed in extracted research); `BusinessApiResponse` envelope per T7; Decisions section 6 (concurrency), 8 (envelopes).

  **Acceptance Criteria**:
  - [ ] Header sent is `Access-Token: <value>` not `Authorization: Bearer <value>` (httpx-inspector assertion)
  - [ ] All test cases use vcrpy cassettes; ≥ 5 unit + integration tests pass
  - [ ] Business API `code != 0` decoded correctly via T7 decoder (Metis 6.6)
  - [ ] Spike S3 cassette replayable through this production client

  **QA Scenarios**:

  ```
  Scenario: Correct Access-Token header (Business API convention)
    Tool: pytest with httpx-inspector
    Steps: pytest tests/integration/test_business_client.py::test_access_token_header -v
    Expected: pass; last_request.headers contains "Access-Token" and NOT "Authorization: Bearer"
    Evidence: .omo/evidence/task-14-header.txt

  Scenario: code != 0 raises BusinessApiError (Metis 6.6)
    Tool: pytest with vcr
    Steps: pytest tests/integration/test_business_client.py::test_code_nonzero_raises -v
    Expected: pass; exception is BusinessApiError with code=<expected>, request_id captured
    Evidence: .omo/evidence/task-14-code-err.txt

  Scenario: Spike S3 cassette compatible with production
    Tool: pytest
    Steps: pytest tests/integration/test_business_client.py::test_spike_s3_compatible -v
    Expected: pass (raises same BusinessApiError as spike prototype)
    Evidence: .omo/evidence/task-14-spike.txt
  ```

  **Commit**: `feat(api/business): add BusinessAPIClient with Access-Token header + envelope decoding` — `src/tiktok_mcp/api/business/client.py`, `tests/integration/test_business_client.py`, `tests/cassettes/business_*.yaml`

- [x] 15. **Marketing API read tools (advertiser/BC/campaign/adgroup/ad listing + info)**

  **What to do**: In `src/tiktok_mcp/tools/marketing_read.py`, register MCP tools (all readOnlyHint) using T14 client. Inventory subset (full list in `docs/api-surface-inventory.md`):
  1. `marketing_list_advertisers(alias: str) -> list[Advertiser]` — `/open_api/v1.3/oauth2/advertiser/get/`
  2. `marketing_get_advertiser_info(alias: str, advertiser_id: str, fields: list[str] | None = None) -> AdvertiserInfo` — `/open_api/v1.3/advertiser/info/`
  3. `marketing_list_business_centers(alias: str) -> list[BusinessCenter]` — `/open_api/v1.3/bc/get/`
  4. `marketing_list_bc_advertisers(alias: str, bc_id: str) -> list[Advertiser]` — `/open_api/v1.3/bc/asset/get/` (or whatever the inventory landed on)
  5. `marketing_list_campaigns(alias: str, advertiser_id: str, filtering: dict | None = None, page: int = 1, page_size: int = 50) -> dict` — `/open_api/v1.3/campaign/get/`
  6. `marketing_list_adgroups(alias: str, advertiser_id: str, filtering: dict | None = None, page: int = 1, page_size: int = 50) -> dict` — `/open_api/v1.3/adgroup/get/`
  7. `marketing_list_ads(alias: str, advertiser_id: str, filtering: dict | None = None, page: int = 1, page_size: int = 50) -> dict` — `/open_api/v1.3/ad/get/`
  8. `marketing_get_campaign(alias: str, advertiser_id: str, campaign_id: str, fields: list[str] | None = None) -> Campaign` — combined variant of #5 with single-id filter
  9. `marketing_get_adgroup(alias, advertiser_id, adgroup_id, fields) -> AdGroup` — same pattern
  10. `marketing_get_ad(alias, advertiser_id, ad_id, fields) -> Ad` — same pattern

  Pydantic models in `src/tiktok_mcp/api/marketing/models.py`. Field lists from research extract; if any field is unclear, omit (Wave 4 polish can add).

  **Must NOT do**: aggregate metrics across currencies (out per Decisions 10); cache responses (stateless); abstract pagination (native passthrough per Decisions 9).

  **Recommended Agent**: `unspecified-high`. Pattern is highly templated — once T15 lands, T16 should follow the same shape with mostly mechanical work.

  **Parallelization**: Parallel with T13, T16-T19. Blocks T20-T22 (write tools mirror these endpoints). Blocked by T14.

  **References**: bg_f4ba0e78 thinking transcript (Marketing API endpoint paths); `docs/api-surface-inventory.md` for the canonical row set.

  **Acceptance Criteria**:
  - [ ] 10 tools registered; matches inventory rows for "Marketing — read"
  - [ ] vcrpy cassette per endpoint
  - [ ] Pagination passthrough: response includes `page`, `page_size`, `total_number`, `total_page` from upstream
  - [ ] Multi-account isolation verified

  **QA Scenarios**:

  ```
  Scenario: All 10 read tools cassette-replay
    Tool: pytest
    Steps: pytest tests/integration/test_marketing_read.py -v
    Expected: ≥ 10 tests pass
    Evidence: .omo/evidence/task-15-reads.txt

  Scenario: Pagination shape preserved
    Tool: pytest
    Steps: pytest tests/integration/test_marketing_read.py::test_pagination -v
    Expected: pass; response contains native page/page_size/total_number
    Evidence: .omo/evidence/task-15-pagination.txt
  ```

  **Commit**: `feat(tools/marketing): add read tools (advertiser, BC, campaign/adgroup/ad list+get)` — `src/tiktok_mcp/tools/marketing_read.py`, `src/tiktok_mcp/api/marketing/models.py`, `tests/integration/test_marketing_read.py`, `tests/cassettes/marketing_*.yaml`

- [x] 16. **Marketing API report tools (sync + async + poll + download)**

  **What to do**: In `src/tiktok_mcp/tools/marketing_reports.py`:
  1. `marketing_run_sync_report(alias, advertiser_id, report_type, data_level, dimensions, metrics, start_date, end_date, filters=None, order_field=None, order_type=None, page=1, page_size=20) -> dict` — `/open_api/v1.3/report/integrated/get/`. Report_type one of BASIC / AUDIENCE / PLAYABLE_AD. Data_level one of AUCTION_AD / AUCTION_ADGROUP / AUCTION_CAMPAIGN / AUCTION_ADVERTISER. **Returns rows with explicit `currency_code` + `timezone` per Decisions 10**.
  2. `marketing_run_async_report(alias, advertiser_id, ...same params...) -> dict` — `/open_api/v1.3/report/task/create/`. Returns `{ task_id, status: "queued" }`.
  3. `marketing_poll_async_report(alias, advertiser_id, task_id) -> dict` — `/open_api/v1.3/report/task/check/`. Returns `{ status, progress_percentage, file_url|null, expires_at|null }`.
  4. `marketing_download_async_report(alias, advertiser_id, task_id) -> dict` — downloads from `file_url` (TikTok-served URL); returns `{ rows: list[dict], row_count, currency_code, timezone }`. Streams + parses CSV; never persists.

  Pydantic models for report params validation. Use `Literal[...]` types for report_type/data_level to constrain options.

  **Must NOT do**: persist downloaded report rows to disk; allow date ranges > documented max (validate `start_date`/`end_date` vs research-extracted limits, e.g. 30 days for some report types); normalize timezone (passthrough only).

  **Recommended Agent**: `unspecified-high`.

  **Parallelization**: Parallel with T13, T15, T17, T18, T19. Blocks none directly. Blocked by T14.

  **References**: bg_f4ba0e78 (full dimensions + metrics tables — surface in `docs/api-surface-inventory.md` open-issues section if uncertain); Decisions section 10.

  **Acceptance Criteria**:
  - [ ] 4 tools registered
  - [ ] Each row in sync-report response has `currency_code` + `timezone` (assertion test)
  - [ ] Async polling test: create → poll (status=queued) → poll (status=success) → download → row count > 0
  - [ ] Invalid report_type rejected at pydantic validation (not at TikTok)
  - [ ] Date range over documented limit rejected with clear error

  **QA Scenarios**:

  ```
  Scenario: Sync report with explicit currency+timezone per row
    Tool: pytest
    Steps: pytest tests/integration/test_marketing_reports.py::test_sync_with_currency -v
    Expected: pass; every row in response.list has currency_code AND timezone keys
    Evidence: .omo/evidence/task-16-currency.txt

  Scenario: Async report full lifecycle
    Tool: pytest with vcr (multi-cassette)
    Steps: pytest tests/integration/test_marketing_reports.py::test_async_lifecycle -v
    Expected: pass; create task_id, 2 polls show progress, final poll returns file_url, download returns parsed rows
    Evidence: .omo/evidence/task-16-async.txt

  Scenario: Invalid report_type rejected at pydantic
    Tool: pytest
    Steps: pytest tests/integration/test_marketing_reports.py::test_invalid_report_type -v
    Expected: pass; ValidationError raised before any HTTP call (httpx-inspector confirms zero requests)
    Evidence: .omo/evidence/task-16-validation.txt
  ```

  **Commit**: `feat(tools/marketing): add report tools (sync + async create/poll/download)` — `src/tiktok_mcp/tools/marketing_reports.py`, `tests/integration/test_marketing_reports.py`, `tests/cassettes/marketing_report_*.yaml`

- [x] 17. **Business Organic comment read tools (list_comments, list_replies, get_comment)**

  **What to do**: In `src/tiktok_mcp/tools/comments_read.py`, register MCP read tools (all readOnlyHint) using T14 Business client. Per bg_066e9675 findings (canonical 2026 paths to be confirmed by inventory):
  1. `comments_list(alias: str, advertiser_id: str, post_id: str, page: int = 1, page_size: int = 30, sort_by: Literal["newest","top"] = "newest") -> dict` — list comments on a video owned by the authorized user. Returns `{ comments: list[Comment], page, page_size, total }`.
  2. `comments_list_replies(alias: str, advertiser_id: str, post_id: str, comment_id: str, page: int = 1, page_size: int = 30) -> dict` — list replies under a specific comment.
  3. `comments_get(alias: str, advertiser_id: str, post_id: str, comment_id: str) -> Comment` — single comment by id (if endpoint exists; otherwise omit and add to v0.2).

  Pydantic `Comment` model with: comment_id, parent_comment_id (None for top-level), author (open_id, display_name, avatar_url), text, like_count, reply_count, create_time, is_top_pinned, is_hidden_by_owner, is_deleted_by_author.

  **Must NOT do**: persist or log comment text bodies (per Decisions 13); cache responses; gate behind any `TIKTOK_MCP_LOG_COMMENT_BODIES` for normal log levels — only debug-level + the env var enabled together dumps bodies; default redactor masks comment text in log lines.

  **Recommended Agent**: `unspecified-high`. Note: this surface had research-agent failure (bg_1baf01b3); the implementing agent MUST verify exact endpoint paths via re-research (`librarian` agent call) before coding — the inventory (T9) is the authoritative source.

  **Parallelization**: Parallel with T13, T15, T16, T18, T19. Blocks T25 (comment writes). Blocked by T14.

  **References**: bg_066e9675 focused research output (if available); `docs/api-surface-inventory.md` for canonical paths; Decisions section 13 (PII).

  **Acceptance Criteria**:
  - [ ] 3 tools registered (or 2 if `comments_get` not available — note in plan addendum)
  - [ ] vcrpy cassettes with comment-body scrubbing applied via `before_record_response` (Decisions 13)
  - [ ] Pytest test asserts comment-text bodies do NOT appear in caplog at INFO level

  **QA Scenarios**:

  ```
  Scenario: list_comments happy path with cassette
    Tool: pytest
    Steps: pytest tests/integration/test_comments_read.py::test_list_happy -v
    Expected: pass; ≥ 1 comment returned with all expected fields
    Evidence: .omo/evidence/task-17-list.txt

  Scenario: Comment text never logged at INFO (PII protection)
    Tool: pytest with caplog
    Steps: pytest tests/integration/test_comments_read.py::test_no_body_in_log -v
    Expected: pass; caplog at INFO level contains comment_id but NOT comment text body
    Evidence: .omo/evidence/task-17-pii.txt

  Scenario: Cassette has scrubbed comment bodies
    Tool: Bash (grep)
    Steps: grep -E "text:.{50,}" tests/cassettes/comments_list.yaml
    Expected: exit code 1 (no matches; bodies were scrubbed to short placeholder strings)
    Evidence: .omo/evidence/task-17-scrub.txt
  ```

  **Commit**: `feat(tools/comments): add comment read tools with PII scrubbing in cassettes` — `src/tiktok_mcp/tools/comments_read.py`, `src/tiktok_mcp/api/business/comment_models.py`, `tests/integration/test_comments_read.py`, `tests/cassettes/comments_*.yaml`

- [x] 18. **Content Posting API read tools (status polling, drafts, creator info)**

  **What to do**: In `src/tiktok_mcp/tools/posting_read.py`, register MCP read tools using a NEW `src/tiktok_mcp/api/posting/client.py` client (Login-Kit-style auth like Display, but separate base URL paths and scopes):
  1. `posting_get_post_status(alias: str, publish_id: str) -> PostStatus` — `/v2/post/publish/status/fetch/`. Returns `{ status: "PROCESSING_DOWNLOAD"|"PROCESSING_UPLOAD"|"PUBLISH_COMPLETE"|"FAILED"|..., uploaded_bytes, video_seconds, publicaly_available_post_id, fail_reason | null }`.
  2. `posting_list_drafts(alias: str, max_count: int = 20, cursor: int | None = None) -> dict` — lists posts in the user's drafts inbox; pagination per Decisions 9. (Endpoint TBD — confirm via T9 inventory; if no list endpoint exists, the tool returns "endpoint_not_available" and is removed from registration with a note in inventory open-issues.)
  3. `posting_get_creator_info(alias: str) -> CreatorInfo` — `/v2/post/publish/creator_info/query/` — returns the creator's allowed privacy options, max video duration, etc. Required before initiating an upload (different creators have different max durations + privacy modes).

  New `PostingAPIClient` (lighter than T12 — shares Login Kit auth semantics; reuse refresh path patterns from T12). Could factor a shared `class LoginKitAuthMixin` if Display+Posting clients share more code than they differ; otherwise keep separate to avoid premature abstraction (per Must NOT Have).

  **Must NOT do**: cache `creator_info` (privacy options can change in TikTok app); abstract Display+Posting into one client unless ≥ 5 shared methods (avoid premature abstraction).

  **Recommended Agent**: `unspecified-high`. Note: status polling is the user-facing pattern for chunked upload progress (T26); this tool MUST be solid before T26 lands.

  **Parallelization**: Parallel with T13, T15, T16, T17, T19. Blocks T26-T28 (Content Posting writes that depend on status polling). Blocked by T3, T4, T5, T7.

  **References**: bg_32690cb8 extract → Content Posting API endpoint paths + status enum; Decisions section 2 (Content Posting IN).

  **Acceptance Criteria**:
  - [ ] 3 tools registered (or 2 if list_drafts unavailable per inventory)
  - [ ] vcrpy cassettes for each
  - [ ] `posting_get_post_status` returns typed `PostStatus` model with status enum validated
  - [ ] `creator_info` includes `privacy_level_options` field (sanity check on response shape)

  **QA Scenarios**:

  ```
  Scenario: Status polling returns typed status enum
    Tool: pytest
    Steps: pytest tests/integration/test_posting_read.py::test_status_enum -v
    Expected: pass; PostStatus.status is one of the documented enum values
    Evidence: .omo/evidence/task-18-status.txt

  Scenario: creator_info shape sanity
    Tool: pytest
    Steps: pytest tests/integration/test_posting_read.py::test_creator_info_shape -v
    Expected: pass; response has privacy_level_options, max_video_post_duration_sec, comment_disabled_supported
    Evidence: .omo/evidence/task-18-creator.txt
  ```

  **Commit**: `feat(tools/posting): add post status + drafts + creator_info read tools` — `src/tiktok_mcp/api/posting/client.py`, `src/tiktok_mcp/api/posting/models.py`, `src/tiktok_mcp/tools/posting_read.py`, `tests/integration/test_posting_read.py`, `tests/cassettes/posting_*.yaml`

- [x] 19. **`get_rate_limit_status` MCP tool (observability)**

  **What to do**: In `src/tiktok_mcp/tools/rate_limit.py`:
  1. `get_rate_limit_status(alias: str | None = None) -> dict` — readOnlyHint. Returns per-account-per-api recent rate-limit posture: `{ accounts: [{ alias, api_type, last_429_at: datetime|null, last_retry_after: int|null, projected_backoff_until: datetime|null, recent_request_count_last_60s: int }] }`. If `alias` provided, returns just that account; else all accounts.
  - Backing store: an in-memory `dict[(api_type, alias), RateLimitPosture]` updated by T12/T14 clients on every request (rolling counter) and on every 429 (timestamp + Retry-After). No persistence; resets on MCP restart.
  - Implementation note: the recording side lives in a new module `src/tiktok_mcp/observability/rate_limit_tracker.py` that the clients import; the tool just reads.

  **Must NOT do**: persist counters (overkill); use distributed cache (single-process MCP); leak account tokens in any error path.

  **Recommended Agent**: `unspecified-high`.

  **Parallelization**: Parallel with T13, T15-T18. Blocks none. Blocked by T3 (uses types) + T12, T14 (clients write to tracker). Note: T19 could in principle land BEFORE T12/T14 (tracker is independent); but for the QA scenarios to work, at least one client needs to be writing to the tracker.

  **References**: Decisions section 7 (reactive rate limit); `get_rate_limit_status` referenced in section 5 (account management tool table).

  **Acceptance Criteria**:
  - [ ] Tool registered
  - [ ] After a forced 429 from T12 or T14 test, `get_rate_limit_status` reflects the event within 100ms
  - [ ] `alias=None` returns all accounts; `alias=<existing>` returns just that one; `alias=<unknown>` returns empty list
  - [ ] No persistence side effects (test: restart MCP between calls, second call returns empty)

  **QA Scenarios**:

  ```
  Scenario: 429 from client recorded in tracker
    Tool: pytest
    Steps: pytest tests/integration/test_rate_limit.py::test_429_recorded -v
    Expected: pass; after T12 client forced-429 test runs, get_rate_limit_status returns last_429_at within last 5s
    Evidence: .omo/evidence/task-19-record.txt

  Scenario: Alias filter
    Tool: pytest
    Steps: pytest tests/integration/test_rate_limit.py::test_alias_filter -v
    Expected: pass; alias filter narrows to single account
    Evidence: .omo/evidence/task-19-filter.txt

  Scenario: No persistence across MCP restarts
    Tool: pytest with subprocess MCP boot
    Steps: pytest tests/integration/test_rate_limit.py::test_no_persistence -v
    Expected: pass; counters reset on fresh boot
    Evidence: .omo/evidence/task-19-no-persist.txt
  ```

  **Commit**: `feat(observability): add get_rate_limit_status tool + in-memory tracker` — `src/tiktok_mcp/observability/rate_limit_tracker.py`, `src/tiktok_mcp/tools/rate_limit.py`, `tests/integration/test_rate_limit.py`

- [x] 20. Marketing API write tools: Campaign CRUD

  **What to do**:
  - `tools/marketing_writes_campaigns.py` exposing `create_campaign`, `update_campaign`, `update_campaign_status` (enable/disable/pause/resume), `delete_campaign` against `/open_api/v1.3/campaign/create/`, `/campaign/update/`, `/campaign/status/update/`, `/campaign/delete/`.
  - All four tools annotated `destructiveHint: true` and decorated `@require_writes_enabled("marketing")`.
  - Use the shared `MarketingApiClient` from T14 (Access-Token header, BusinessApiResponse decoder).
  - Input pydantic models: `CreateCampaignRequest` (`advertiser_id`, `campaign_name`, `objective_type`, `budget_mode`, `budget`, `app_promotion_type` optional, `special_industries` optional), `UpdateCampaignRequest` (subset writable fields), `CampaignStatusUpdate` (`campaign_ids: list[str]`, `operation_status: Literal["ENABLE","DISABLE","DELETE"]`).
  - Return shape: `{ campaign_id, modify_time, status }` (do NOT return advertiser-wide list).
  - Each write logs at INFO: `{action, advertiser_id, campaign_id, request_id, would_have_done?}` (the `would_have_done` field is populated only on the blocked path).

  **Must NOT do**:
  - Implement reservation buying or DPA campaigns (out of v0.1 scope).
  - Allow cross-advertiser bulk operations in a single call.
  - Cache `advertiser_id → campaign_id` mappings.
  - Echo `budget` or `app_promotion_type` into log lines at DEBUG level (potential business sensitivity).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: write-side API surface, must enforce env-var gate + destructive annotations + atomic single-campaign mutation
  - **Skills**: `customize-opencode` (only if MCP tool registration patterns need confirmation), none mandatory
  - **Skills Evaluated but Omitted**: `web-design-guidelines` (server-side), `accessibility` (server-side)

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 with T21, T22, T23, T24, T25, T26, T27, T28
  - **Blocks**: nothing in Wave 3 (siblings independent); Wave 4 doc tasks reference these tools
  - **Blocked By**: T14 (Marketing client), T8 (write-gate decorator), T7 (BusinessApiResponse decoder)

  **References**:
  - Decisions of Record §4 (write gating + destructive annotations) and §13 (Business response envelope)
  - `src/tiktok_mcp/marketing/client.py` (built in T14) — call helpers
  - `src/tiktok_mcp/auth/write_gate.py` (built in T8) — `@require_writes_enabled` decorator usage
  - External: `https://business-api.tiktok.com/portal/docs?id=1739318962329602` (Campaign Create) and `?id=1739318708928513` (Campaign Update) — exact field lists
  - Cassette pattern: `tests/cassettes/marketing_campaigns/*.yaml` recorded via `vcrpy` against sandbox advertiser `7642629596042543111` with token from Wave-1 spike credentials

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_campaigns_writes.py -k "test_create_campaign_blocked_when_writes_disabled" → PASS` covering 7 env-var truthy/falsy values
  - [ ] `uv run pytest tests/unit/test_campaigns_writes.py -k "test_create_campaign_routes_marketing_only" → PASS` (sets `TIKTOK_MCP_ALLOW_WRITES=comments`; expects `writes_disabled`)
  - [ ] `uv run pytest tests/integration/test_campaigns_writes_replay.py → PASS` replays cassette for create+update+status+delete
  - [ ] `uv run pytest tests/unit/test_campaigns_writes.py -k "destructive_hint" → PASS` (introspects FastMCP tool registry, asserts all 4 tools advertise `destructiveHint=True`)
  - [ ] `rg "would_have_done" src/tiktok_mcp/tools/marketing_writes_campaigns.py` returns ≥1 line per blocked path

  **QA Scenarios** (MANDATORY):

  ```
  Scenario: Blocked when TIKTOK_MCP_ALLOW_WRITES unset
    Tool: interactive_bash (tmux)
    Preconditions: env unset; in-memory MCP server running; sandbox account `nordic-no-test` registered
    Steps:
      1. Send JSON-RPC `tools/call` with `{name: "create_campaign", arguments: {alias: "nordic-no-test", advertiser_id: "7642629596042543111", campaign_name: "QA-TEST", objective_type: "TRAFFIC", budget_mode: "BUDGET_MODE_DAY", budget: 50}}`
      2. Capture full response
      3. Assert response.error.code == "writes_disabled"
      4. Assert response.error.message contains "TIKTOK_MCP_ALLOW_WRITES"
      5. Assert response.error.would_have_done.endpoint == "/open_api/v1.3/campaign/create/"
    Expected Result: structured error; zero outbound HTTP to TikTok (httpx-inspector last_request is None)
    Failure Indicators: any 200 response, any outbound HTTP request, any token redaction failure in logs
    Evidence: .omo/evidence/task-20-blocked-writes-disabled.json

  Scenario: Allowed when TIKTOK_MCP_ALLOW_WRITES=marketing
    Tool: interactive_bash (tmux) + vcrpy cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`; cassette `marketing_campaigns/create_traffic.yaml` placed
    Steps:
      1. Send same JSON-RPC `tools/call`
      2. Assert response.result.campaign_id matches cassette response (e.g. `"1733456789012345"`)
      3. Assert response.result.status == "ENABLE"
      4. Assert outbound request `Access-Token` header equals stored token
      5. Assert outbound request URL == "https://business-api.tiktok.com/open_api/v1.3/campaign/create/"
    Expected Result: success; campaign id returned; tool log at INFO contains `{"action":"campaign.create","advertiser_id":"7642629596042543111","campaign_id":"...","request_id":"..."}`
    Evidence: .omo/evidence/task-20-allowed-marketing.json
  ```

  **Commit**: `feat(marketing): campaign CRUD write tools (env-gated, destructive)` — `src/tiktok_mcp/tools/marketing_writes_campaigns.py`, `src/tiktok_mcp/marketing/models_writes.py`, `tests/unit/test_campaigns_writes.py`, `tests/integration/test_campaigns_writes_replay.py`, `tests/cassettes/marketing_campaigns/*.yaml`

- [x] 21. Marketing API write tools: AdGroup CRUD

  **What to do**:
  - `tools/marketing_writes_adgroups.py` exposing `create_adgroup`, `update_adgroup`, `update_adgroup_status`, `delete_adgroup` against `/open_api/v1.3/adgroup/create/`, `/adgroup/update/`, `/adgroup/status/update/`, `/adgroup/delete/`.
  - All four tools `destructiveHint: true` + `@require_writes_enabled("marketing")`.
  - `CreateAdGroupRequest` covers required fields per TikTok docs (placement_type, schedule_type, billing_event, optimization_goal, bid_type, bid_price/budget, audience_ids optional, targeting block: locations/genders/age_groups/languages/interests/behaviors/operating_systems/network_types).
  - Targeting block accepts Nordic country codes: `NO`, `SE`, `DK`, `FI`. Validate at pydantic layer.
  - `update_adgroup_status` accepts `operation_status: Literal["ENABLE","DISABLE","DELETE"]` mirroring T20.

  **Must NOT do**:
  - Implement audience-segments creation here (T23 owns it).
  - Auto-create campaigns if `campaign_id` missing — return validation error instead.
  - Default any geo to a wide region (e.g. "ALL") — must be explicit list.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: large field surface, validation-heavy, must enforce destructive annotations
  - **Skills**: none mandatory
  - **Skills Evaluated but Omitted**: `seo` (irrelevant)

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20, T22, T23, T24, T25, T26, T27, T28
  - **Blocks**: nothing
  - **Blocked By**: T14, T8, T7

  **References**:
  - External: `https://business-api.tiktok.com/portal/docs?id=1739499616346114` (Ad Group Create), `?id=1739499773233666` (Update)
  - `src/tiktok_mcp/marketing/models_writes.py` (extended from T20)
  - Decisions of Record §4, §13

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_adgroups_writes.py -k "test_blocked" → PASS` (7 env-var values)
  - [ ] `uv run pytest tests/unit/test_adgroups_writes.py -k "test_geo_validation" → PASS` (rejects `["XX"]`, accepts `["NO","SE"]`)
  - [ ] `uv run pytest tests/integration/test_adgroups_writes_replay.py → PASS` (cassette-driven happy path for create+update+status+delete)
  - [ ] All 4 tools advertise `destructiveHint=True` in tool-registry introspection test

  **QA Scenarios**:

  ```
  Scenario: Reject invalid Nordic geo code at validation layer
    Tool: interactive_bash (tmux)
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`
    Steps:
      1. JSON-RPC `tools/call` create_adgroup with `targeting.locations: ["XX"]`
      2. Assert response.error.code == "validation_error"
      3. Assert response.error.message contains "locations"
      4. Assert NO outbound HTTP request fired
    Expected Result: 422-style validation error before client call
    Evidence: .omo/evidence/task-21-invalid-geo.json

  Scenario: Pause adgroup via update_adgroup_status
    Tool: interactive_bash (tmux) + cassette
    Preconditions: cassette `marketing_adgroups/pause.yaml` placed
    Steps:
      1. JSON-RPC `tools/call` update_adgroup_status with `adgroup_ids: ["X","Y"]`, `operation_status: "DISABLE"`
      2. Assert response.result.success_count == 2
      3. Assert outbound URL == ".../adgroup/status/update/"
    Expected Result: success; log line contains both adgroup ids
    Evidence: .omo/evidence/task-21-pause-adgroups.json
  ```

  **Commit**: `feat(marketing): adgroup CRUD write tools` — `src/tiktok_mcp/tools/marketing_writes_adgroups.py`, models extension, tests, cassettes

- [x] 22. Marketing API write tools: Ad CRUD

  **What to do**:
  - `tools/marketing_writes_ads.py` exposing `create_ad`, `update_ad`, `update_ad_status`, `delete_ad` against `/open_api/v1.3/ad/create/`, `/ad/update/`, `/ad/status/update/`, `/ad/delete/`.
  - All four `destructiveHint: true` + `@require_writes_enabled("marketing")`.
  - `CreateAdRequest` requires `adgroup_id`, `creative_material_mode` ("CUSTOM"), `ad_name`, `ad_format` ("SINGLE_VIDEO"|"COLLECTION_ADS"|"CATALOG_CAROUSEL"), `identity_type`, `identity_id`, `video_id` or `image_ids`, `ad_text`, `landing_page_url` optional, `call_to_action` optional, `display_name` optional.
  - Accepts `creative_authorized: bool` flag; reject if user passes `true` without `spark_ads_post_id` (spark-ads requires authorized creator post).

  **Must NOT do**:
  - Implement DPA carousel auto-population (deferred to v0.2).
  - Bulk-create ads in a loop within one tool call (return id-per-call only).
  - Accept `video_id` from outside the same advertiser (validate via Wave-2 read tools if needed).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20, T21, T23, T24, T25, T26, T27, T28
  - **Blocks**: nothing
  - **Blocked By**: T14, T8, T7

  **References**:
  - External: `https://business-api.tiktok.com/portal/docs?id=1739953377508354` (Ad Create), `?id=1740031477835777` (Update)
  - Decisions of Record §4, §13

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_ads_writes.py -k "test_blocked" → PASS`
  - [ ] `uv run pytest tests/unit/test_ads_writes.py -k "test_spark_ads_requires_post_id" → PASS`
  - [ ] `uv run pytest tests/integration/test_ads_writes_replay.py → PASS`
  - [ ] All 4 tools `destructiveHint=True` (registry introspection)

  **QA Scenarios**:

  ```
  Scenario: Create ad referencing video uploaded via Content Posting
    Tool: interactive_bash + cassette
    Preconditions: cassette `marketing_ads/create_single_video.yaml`; `video_id` from prior Content Posting flow recorded in cassette setup
    Steps:
      1. JSON-RPC `tools/call` create_ad with `ad_format: "SINGLE_VIDEO"`, `creative_material_mode: "CUSTOM"`, `video_id: "<cassette-id>"`
      2. Assert response.result.ad_id matches cassette
      3. Assert outbound body contains `creative_material_mode: "CUSTOM"`
    Expected Result: ad created; log contains ad_id
    Evidence: .omo/evidence/task-22-create-ad-single-video.json

  Scenario: Reject spark-ads with no spark_ads_post_id
    Tool: interactive_bash
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`
    Steps:
      1. JSON-RPC create_ad with `creative_authorized: true`, no `spark_ads_post_id`
      2. Assert response.error.code == "validation_error"
      3. Assert no outbound HTTP
    Expected Result: pre-flight rejection
    Evidence: .omo/evidence/task-22-spark-ads-missing-id.json
  ```

  **Commit**: `feat(marketing): ad CRUD write tools` — `src/tiktok_mcp/tools/marketing_writes_ads.py`, models, tests, cassettes

- [x] 23. Marketing API write tools: Custom Audience uploads

  **What to do**:
  - `tools/marketing_writes_audiences.py` exposing `create_custom_audience` (file upload), `update_custom_audience_name`, `delete_custom_audience` against `/open_api/v1.3/dmp/custom_audience/create/`, `/dmp/custom_audience/update/`, `/dmp/custom_audience/delete/`.
  - File-upload path: accepts a local file path argument (`source_file_path: str`); MCP reads file from disk, hashes per TikTok spec (SHA-256 for emails/phones, lowercased + trimmed), constructs multipart body, posts.
  - Reject file paths outside the user's home dir or with traversal segments (`..`) at validation layer.
  - File >100MB rejected pre-flight with clear `audience_file_too_large` error.
  - `update_custom_audience_name` and `delete_custom_audience` are JSON endpoints, not multipart.
  - All three `destructiveHint: true` + `@require_writes_enabled("marketing")`.

  **Must NOT do**:
  - Implement Lookalike Audience creation (deferred to v0.2).
  - Persist or cache hashed audience records on disk — hash once in memory, post, discard.
  - Log the file path at INFO level (it may contain PII in the filename); log only `{filename_hash, row_count_estimate, file_size_bytes}`.
  - Accept network URLs as `source_file_path` (file-system only).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: file IO, hashing, multipart construction, PII-handling carefulness
  - **Skills**: none mandatory
  - **Skills Evaluated but Omitted**: `cso` (server-side, runtime; this is build-time)

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T22, T24-T28
  - **Blocks**: nothing
  - **Blocked By**: T14, T8

  **References**:
  - External: `https://business-api.tiktok.com/portal/docs?id=1739940561492481` (Custom Audience file upload)
  - Decisions of Record §4 (writes), §16 (PII handling — extend to audience files)
  - Hashing spec: SHA-256, lowercase, trim whitespace, no separators in phone numbers

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_audiences_writes.py -k "test_blocked" → PASS`
  - [ ] `uv run pytest tests/unit/test_audiences_writes.py -k "test_path_traversal_rejected" → PASS`
  - [ ] `uv run pytest tests/unit/test_audiences_writes.py -k "test_hash_format" → PASS` (asserts test fixture file `tests/fixtures/audiences/sample_emails.csv` hashes match TikTok spec)
  - [ ] `uv run pytest tests/unit/test_audiences_writes.py -k "test_no_pii_in_logs" → PASS` (caplog assertion)
  - [ ] `uv run pytest tests/integration/test_audiences_writes_replay.py → PASS`

  **QA Scenarios**:

  ```
  Scenario: Upload custom audience from CSV
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`; fixture `tests/fixtures/audiences/sample_emails.csv` with 100 rows; cassette `marketing_audiences/create_upload.yaml`
    Steps:
      1. JSON-RPC `tools/call` create_custom_audience with `source_file_path: "tests/fixtures/audiences/sample_emails.csv"`, `audience_name: "qa-test-100"`, `match_keys: ["email"]`
      2. Assert response.result.custom_audience_id is non-empty
      3. Assert outbound request body fields (multipart) include hashed values, NOT plaintext emails
      4. Grep caplog for "[email protected]" → expect zero matches
    Expected Result: success; no PII in logs; audience id returned
    Evidence: .omo/evidence/task-23-upload-audience.json, .omo/evidence/task-23-no-pii-in-logs.txt

  Scenario: Reject path traversal attempt
    Tool: interactive_bash
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`
    Steps:
      1. JSON-RPC create_custom_audience with `source_file_path: "../../../etc/passwd"`
      2. Assert response.error.code == "invalid_path"
      3. Assert no outbound HTTP
    Expected Result: pre-flight rejection
    Evidence: .omo/evidence/task-23-path-traversal.json
  ```

  **Commit**: `feat(marketing): custom audience upload tools (PII-hashed, env-gated)` — `src/tiktok_mcp/tools/marketing_writes_audiences.py`, `src/tiktok_mcp/marketing/audience_hashing.py`, `tests/unit/test_audiences_writes.py`, `tests/fixtures/audiences/sample_emails.csv`, cassettes

- [x] 24. Marketing API write tools: Creative asset uploads

  **What to do**:
  - `tools/marketing_writes_creatives.py` exposing `upload_video_asset`, `upload_image_asset`, `delete_video_asset`, `delete_image_asset` against `/open_api/v1.3/file/video/ad/upload/`, `/file/image/ad/upload/`, `/file/video/ad/delete/`, `/file/image/ad/delete/`.
  - Video upload accepts `source_file_path` + `advertiser_id`; chunks at 5MB-64MB; returns `{video_id, video_signature, size, format, height, width, bit_rate, duration, file_name}`.
  - Image upload accepts `source_file_path` + `advertiser_id`; single-shot multipart; returns `{image_id, image_url, signature, size, format, height, width}`.
  - Delete tools accept `advertiser_id` + `video_ids`/`image_ids: list[str]`.
  - All four `destructiveHint: true` (creates persistent assets on TikTok's side; delete is permanent) + `@require_writes_enabled("marketing")`.
  - Compute SHA-256 of file pre-upload; pass `video_signature`/`image_signature` to TikTok (required for dedup).

  **Must NOT do**:
  - Implement video-from-URL ingestion here (Content Posting T27 handles PULL_FROM_URL).
  - Re-upload if `video_signature` already exists — return existing `video_id` from TikTok's dedup response.
  - Auto-delete on test failure (test cleanup is a separate concern; orphans are user-managed).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T23, T25-T28
  - **Blocks**: nothing
  - **Blocked By**: T14, T8

  **References**:
  - External: `https://business-api.tiktok.com/portal/docs?id=1739940641162753` (Creative library video upload)
  - Decisions of Record §4

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_creatives_writes.py -k "test_blocked" → PASS`
  - [ ] `uv run pytest tests/unit/test_creatives_writes.py -k "test_chunking" → PASS` (asserts file >5MB splits into ≥2 chunks)
  - [ ] `uv run pytest tests/integration/test_creatives_writes_replay.py → PASS`
  - [ ] All 4 tools `destructiveHint=True`

  **QA Scenarios**:

  ```
  Scenario: Upload small image to creative library
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`; fixture `tests/fixtures/creatives/sample.jpg` (~50KB); cassette
    Steps:
      1. JSON-RPC upload_image_asset with `source_file_path: "tests/fixtures/creatives/sample.jpg"`, `advertiser_id: "7642629596042543111"`
      2. Assert response.result.image_id non-empty
      3. Assert response.result.format == "JPG"
    Expected Result: image registered; id returned
    Evidence: .omo/evidence/task-24-upload-image.json

  Scenario: Chunked video upload >5MB
    Tool: interactive_bash + cassette
    Preconditions: fixture `tests/fixtures/creatives/sample_8mb.mp4`; cassette `marketing_creatives/upload_video_chunked.yaml`
    Steps:
      1. JSON-RPC upload_video_asset with the 8MB file
      2. Assert response.result.video_id non-empty
      3. Assert cassette recorded ≥2 chunk POSTs
    Expected Result: chunks combine into single asset
    Evidence: .omo/evidence/task-24-upload-video-chunked.json
  ```

  **Commit**: `feat(marketing): creative library upload tools` — `src/tiktok_mcp/tools/marketing_writes_creatives.py`, chunking helper, tests, fixtures, cassettes

- [x] 25. Business Organic API: comment moderation write tools

  **What to do**:
  - `tools/comments_writes.py` exposing 6 tools: `post_comment_reply`, `pin_comment`, `unpin_comment`, `hide_comment`, `unhide_comment`, `delete_own_reply`.
  - Endpoints (per Business Organic / Accounts API docs):
    - Reply: `/open_api/v1.3/comment/reply/create/`
    - Pin/unpin: `/open_api/v1.3/comment/pin/` with `action: "PIN"|"UNPIN"`
    - Hide/unhide: `/open_api/v1.3/comment/hide/` with `action: "HIDE"|"UNHIDE"`
    - Delete own reply: `/open_api/v1.3/comment/reply/delete/`
  - All 6 `destructiveHint: true` + `@require_writes_enabled("comments")`.
  - Comment reply text validated: max 150 chars, no surrogates, NFC-normalized.
  - Each tool requires `business_id` (the Business Center ID) + `account_id` (the brand TikTok account); validate the comment_id belongs to a video on `account_id` before call (via cassette-mock or actual lookup).
  - Hide is reversible; delete is permanent — `delete_own_reply` returns `{deleted: true, comment_id, deleted_at}` and logs at WARN.

  **Must NOT do**:
  - Store comment text or reply text on disk or in long-lived in-memory cache (see Decisions §16 PII handling).
  - Log reply text at any level unless `TIKTOK_MCP_LOG_COMMENT_BODIES=1` set (Decisions §16).
  - Implement bulk delete (one comment at a time only).
  - Allow `delete_others_reply` — only own replies are deletable per TikTok policy; reject if attempted.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: PII-sensitive surface, multiple moderation actions, two-axis env gating (writes + per-API comments)
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T24, T26-T28
  - **Blocks**: nothing
  - **Blocked By**: T17 (comment read tools — share `CommentsApiClient`), T8, T7

  **References**:
  - External: TikTok Business Account API → Comment Management (see `bg_066e9675` partial result and `https://business-api.tiktok.com/portal/docs?id=1747977406714881`)
  - Decisions of Record §4 (writes per-API), §16 (PII)
  - `src/tiktok_mcp/comments/client.py` (built in T17)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_comments_writes.py -k "test_blocked_writes" → PASS` (TIKTOK_MCP_ALLOW_WRITES unset → block)
  - [ ] `uv run pytest tests/unit/test_comments_writes.py -k "test_blocked_when_only_marketing" → PASS` (`=marketing` → still blocks comments)
  - [ ] `uv run pytest tests/unit/test_comments_writes.py -k "test_allowed_with_comments_or_all" → PASS` (`=comments` AND `=all` both allow)
  - [ ] `uv run pytest tests/unit/test_comments_writes.py -k "test_reply_text_not_in_log" → PASS` (caplog assertion at INFO/WARN; only present at DEBUG with explicit flag)
  - [ ] `uv run pytest tests/integration/test_comments_writes_replay.py → PASS` covering all 6 tools
  - [ ] `uv run pytest tests/unit/test_comments_writes.py -k "test_reply_max_length" → PASS` (151 chars rejected; 150 accepted)

  **QA Scenarios**:

  ```
  Scenario: Hide then unhide a comment (env=comments)
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=comments`; cassette `comments_writes/hide_unhide.yaml`
    Steps:
      1. JSON-RPC hide_comment with `business_id`, `account_id`, `comment_id: "fixture-cid-1"`
      2. Assert response.result.action == "HIDE" and response.result.comment_id matches
      3. JSON-RPC unhide_comment with same comment_id
      4. Assert response.result.action == "UNHIDE"
    Expected Result: both actions succeed; log lines do not contain comment body text
    Evidence: .omo/evidence/task-25-hide-unhide.json, .omo/evidence/task-25-no-body-leak.txt

  Scenario: Reply text >150 chars rejected pre-flight
    Tool: interactive_bash
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=comments`
    Steps:
      1. JSON-RPC post_comment_reply with `reply_text` = 151 chars of "a"
      2. Assert response.error.code == "validation_error"
      3. Assert no outbound HTTP request fired
    Expected Result: pre-flight rejection
    Evidence: .omo/evidence/task-25-reply-too-long.json

  Scenario: Per-API gating — marketing enabled, comments blocked
    Tool: interactive_bash
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=marketing`
    Steps:
      1. JSON-RPC hide_comment with valid fixture args
      2. Assert response.error.code == "writes_disabled"
      3. Assert response.error.would_have_done.api == "comments"
    Expected Result: blocked despite marketing being enabled
    Evidence: .omo/evidence/task-25-per-api-gating.json
  ```

  **Commit**: `feat(comments): comment moderation write tools (per-API gated, PII-safe)` — `src/tiktok_mcp/tools/comments_writes.py`, `tests/unit/test_comments_writes.py`, `tests/integration/test_comments_writes_replay.py`, `tests/cassettes/comments_writes/*.yaml`

- [x] 26. Content Posting API: chunked video upload (FILE_UPLOAD)

  **What to do**:
  - `tools/posting_writes_video_upload.py` exposing 3 tools forming the FILE_UPLOAD flow:
    - `init_video_upload(alias, video_size: int, chunk_size: int, total_chunk_count: int)` → POSTs `/v2/post/publish/inbox/video/init/` with `source_info.source: "FILE_UPLOAD"` body; returns `{publish_id, upload_url}`.
    - `upload_video_chunk(publish_id, upload_url, chunk_index, chunk_bytes_b64)` → PUTs chunk to `upload_url` with `Content-Range: bytes <start>-<end>/<total>`; idempotent (TikTok returns 200 for already-seen chunks).
    - `finalize_video_upload(publish_id)` → polls `/v2/post/publish/status/fetch/` with `publish_id` until status terminal (`PUBLISH_COMPLETE`, `FAILED`); returns final status + any errors.
  - Chunks must be 5MB-64MB EXCEPT last chunk which may be smaller; pydantic validation enforces.
  - Upload defaults to **draft inbox** (no `post_info` field); user must call `move_draft_to_publish` from T28 to actually post.
  - All 3 tools `destructiveHint: true` (creates uploaded content) + `@require_writes_enabled("posting")`.
  - Token expiry mid-upload: on 401 from `upload_url` PUT, refresh access token via Login Kit (Decisions §12 atomic refresh) and retry chunk once.

  **Must NOT do**:
  - Read the full file into memory; stream chunks via `aiofiles` to keep memory bounded.
  - Hold the keychain lock across the entire upload (only during refresh).
  - Default to `publish_immediately=true` (Decisions §12 — drafts only by default; opt-in is T27's `direct_post_video_from_url` and T28's `move_draft_to_publish`).
  - Persist `publish_id` across MCP restarts — in-memory only; user must re-init if MCP dies mid-upload.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: streaming IO + chunking math + token refresh + idempotency
  - **Skills**: none mandatory
  - **Skills Evaluated but Omitted**: `performance` (no perf goals in v0.1 — correctness first)

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T25, T27, T28
  - **Blocks**: nothing (T28 draft management does NOT require T26 chunked-upload code; draft inbox primitives are minted by T18 Posting client. Status polling logic is shared via the Posting client, not T26 itself.)
  - **Blocked By**: T18 (Posting client), T8 (write gate), T12 (Display client — for token refresh)

  **References**:
  - External: `https://developers.tiktok.com/doc/content-posting-api-reference-upload-video` (FILE_UPLOAD spec)
  - External: `https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status` (status fetch)
  - Decisions of Record §4 (writes), §12 (atomic refresh), §16 (no body content in INFO logs)
  - `src/tiktok_mcp/posting/client.py` (built in T18)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_video_upload_writes.py -k "test_blocked" → PASS` (TIKTOK_MCP_ALLOW_WRITES unset)
  - [ ] `uv run pytest tests/unit/test_video_upload_writes.py -k "test_chunk_math" → PASS` (asserts 8MB file → 1 chunk of 8MB; 70MB file → 2 chunks: 64MB + 6MB)
  - [ ] `uv run pytest tests/unit/test_video_upload_writes.py -k "test_idempotent_retry" → PASS` (re-upload same chunk_index → no error)
  - [ ] `uv run pytest tests/integration/test_video_upload_replay.py → PASS` covering init+chunk+finalize happy path
  - [ ] `uv run pytest tests/unit/test_video_upload_writes.py -k "test_token_refresh_mid_upload" → PASS` (cassette injects 401 on chunk 2, asserts refresh+retry-once; assert chunk uploads with new token)
  - [ ] `uv run pytest tests/unit/test_video_upload_writes.py -k "test_drafts_by_default" → PASS` (init body contains no `post_info` field)

  **QA Scenarios**:

  ```
  Scenario: Upload 8MB video as draft (single chunk)
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=posting`; fixture `tests/fixtures/posting/sample_8mb.mp4`; cassette `posting_video_upload/single_chunk_draft.yaml`
    Steps:
      1. JSON-RPC init_video_upload with `alias`, `video_size: 8388608`, `chunk_size: 8388608`, `total_chunk_count: 1`
      2. Capture publish_id + upload_url
      3. JSON-RPC upload_video_chunk with chunk 0 (full file b64'd)
      4. Assert response.result.status == "CHUNK_UPLOADED"
      5. JSON-RPC finalize_video_upload with publish_id; poll loop returns once status terminal
      6. Assert final status == "PUBLISH_COMPLETE"
      7. Assert no `post_info` block in init request body
    Expected Result: draft visible in TikTok creator inbox (verified via cassette assertion of init body containing no `post_info`)
    Evidence: .omo/evidence/task-26-draft-upload-8mb.json

  Scenario: Token refresh mid-upload (chunk 2/3 returns 401)
    Tool: pytest with vcrpy cassette
    Preconditions: cassette `posting_video_upload/token_refresh_mid_chunk.yaml` records init→chunk0(200)→chunk1(401)→refresh(200)→chunk1(200)→finalize(200); fixture 70MB
    Steps:
      1. Run `pytest tests/integration/test_video_upload_replay.py::test_token_refresh_mid_upload -v`
      2. Assert test passes; assert chunk 1 is requested twice (once with old token, once with new)
      3. Assert keychain contains new refresh_token by end (atomic swap)
    Expected Result: upload completes; new tokens persisted; only ONE refresh occurred
    Evidence: .omo/evidence/task-26-token-refresh.json
  ```

  **Commit**: `feat(posting): chunked video upload tools (drafts default, refresh-aware)` — `src/tiktok_mcp/tools/posting_writes_video_upload.py`, `src/tiktok_mcp/posting/chunker.py`, `tests/unit/test_video_upload_writes.py`, `tests/integration/test_video_upload_replay.py`, fixtures, cassettes

- [x] 27. Content Posting API: PULL_FROM_URL + photo uploads + direct post opt-in

  **What to do**:
  - `tools/posting_writes_pull_and_photo.py` exposing:
    - `upload_video_from_url(alias, video_url, publish_immediately: bool = False, post_info: dict | None = None)` → POSTs `/v2/post/publish/inbox/video/init/` with `source_info.source: "PULL_FROM_URL"`, `source_info.video_url`. If `publish_immediately=True` AND `post_info` provided, posts to `/v2/post/publish/video/init/` (direct post endpoint) instead.
    - `upload_photo_from_urls(alias, photo_urls: list[str], publish_immediately: bool = False, post_info: dict | None = None)` → uses photo init endpoint; up to 35 photos per slideshow.
    - `get_publish_status(publish_id)` → single fetch (not polling); returns current status. Calls the Posting client's `status` method (provided by T18 Posting client, NOT T26 — T26 reuses the same client method but does not own it). T27 has no task-level dependency on T26.
    - `cancel_publish(publish_id)` → POSTs cancel endpoint if still pending.
  - All 4 `destructiveHint: true` + `@require_writes_enabled("posting")`.
  - `publish_immediately=True` requires `post_info` to contain `title`, `privacy_level` (one of: `MUTUAL_FOLLOW_FRIENDS`, `SELF_ONLY`, `PUBLIC_TO_EVERYONE`, `FOLLOWER_OF_CREATOR`); validate at pydantic layer.
  - `video_url` must be HTTPS; reject `http://` and `file://` and `data:`.

  **Must NOT do**:
  - Default `publish_immediately=True` ever (Decisions §12).
  - Pull-from-url to public TikTok user's URL — TikTok will fetch from the provided URL; the user controls the URL, not the MCP.
  - Cache `publish_id` longer than the polling window.
  - Implement interactive slideshow features (deferred to v0.2). NOTE: 'multi-photo upload' (this task) ≠ 'slideshow' (an interactive playback experience that auto-advances frames with music). Multi-photo upload is a static post with multiple photos in TikTok's carousel/photo-post format. Document this distinction in the tool's docstring.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T26, T28
  - **Blocks**: nothing
  - **Blocked By**: T18 (Posting client), T8

  **References**:
  - External: `https://developers.tiktok.com/doc/content-posting-api-reference-direct-post` (Direct Post)
  - External: `https://developers.tiktok.com/doc/content-posting-api-reference-photo-post`
  - Decisions of Record §4, §12

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_pull_photo_writes.py -k "test_blocked" → PASS`
  - [ ] `uv run pytest tests/unit/test_pull_photo_writes.py -k "test_direct_post_requires_post_info" → PASS` (`publish_immediately=True` + missing `post_info` → validation error)
  - [ ] `uv run pytest tests/unit/test_pull_photo_writes.py -k "test_https_only" → PASS` (rejects `http://`, `file://`, `data:`)
  - [ ] `uv run pytest tests/integration/test_pull_photo_replay.py → PASS` covering pull-from-URL draft, photo carousel, cancel
  - [ ] `uv run pytest tests/unit/test_pull_photo_writes.py -k "test_privacy_level_validation" → PASS`

  **QA Scenarios**:

  ```
  Scenario: Pull video from URL as draft (default behavior)
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=posting`; cassette `posting_pull/from_url_draft.yaml`
    Steps:
      1. JSON-RPC upload_video_from_url with `video_url: "https://example.com/sample.mp4"`
      2. Capture publish_id
      3. JSON-RPC get_publish_status with publish_id (cassette returns `FETCH_IN_PROGRESS`)
      4. Assert response.result.status == "FETCH_IN_PROGRESS"
      5. Assert init request body had `source_info.source: "PULL_FROM_URL"` and NO `post_info` block
    Expected Result: draft created; goes to creator inbox per default
    Evidence: .omo/evidence/task-27-pull-draft.json

  Scenario: Direct post requires explicit privacy_level
    Tool: interactive_bash
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=posting`
    Steps:
      1. JSON-RPC upload_video_from_url with `publish_immediately: true`, `post_info: {title: "X"}` (missing privacy_level)
      2. Assert response.error.code == "validation_error"
      3. Assert response.error.message references "privacy_level"
      4. Assert no outbound HTTP
    Expected Result: pre-flight rejection
    Evidence: .omo/evidence/task-27-missing-privacy.json

  Scenario: Multi-photo upload from 3 URLs (still photos as a single post — NOT the v0.2 'interactive slideshow' feature)
    Tool: interactive_bash + cassette
    Preconditions: env `=posting`; cassette
    Steps:
      1. JSON-RPC upload_photo_from_urls with 3 URLs
      2. Assert publish_id returned
      3. Assert outbound body source_info.photo_images.image_urls has length 3
    Expected Result: photo carousel queued
    Evidence: .omo/evidence/task-27-photo-slideshow.json
  ```

  **Commit**: `feat(posting): pull-from-url + photo upload tools (drafts default)` — `src/tiktok_mcp/tools/posting_writes_pull_and_photo.py`, tests, cassettes

- [x] 28. Content Posting API: draft management writes

  **What to do**:
  - `tools/posting_writes_drafts.py` exposing:
    - `move_draft_to_publish(publish_id, post_info: dict)` → calls TikTok's draft-publish endpoint that converts an inbox draft into a published post; requires `title`, `privacy_level`, optional `disable_duet`, `disable_comment`, `disable_stitch`, `video_cover_timestamp_ms`, `auto_add_music`.
    - `delete_draft(publish_id)` → cancels and removes an inbox draft (irreversible).
    - `list_pending_drafts(alias)` → READ-only, included here for cohesion: lists all inbox drafts not yet acted on; `readOnlyHint: true`, not gated.
  - Write tools (`move_draft_to_publish`, `delete_draft`) `destructiveHint: true` + `@require_writes_enabled("posting")`.
  - `list_pending_drafts` `readOnlyHint: true`, NOT gated.

  **Must NOT do**:
  - Allow `move_draft_to_publish` to skip `privacy_level` (must be explicit).
  - Auto-publish drafts on a schedule (no scheduler in v0.1).
  - Delete drafts older than N days automatically (user decides).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 alongside T20-T27
  - **Blocks**: nothing
  - **Blocked By**: T18, T8

  **References**:
  - External: TikTok Content Posting API → draft conversion endpoint (`https://developers.tiktok.com/doc/content-posting-api-reference-direct-post` covers transition semantics)
  - Decisions of Record §4, §12

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_drafts_writes.py -k "test_blocked" → PASS`
  - [ ] `uv run pytest tests/unit/test_drafts_writes.py -k "test_list_drafts_not_gated" → PASS` (env unset; list still works)
  - [ ] `uv run pytest tests/unit/test_drafts_writes.py -k "test_publish_requires_privacy_level" → PASS`
  - [ ] `uv run pytest tests/integration/test_drafts_writes_replay.py → PASS`

  **QA Scenarios**:

  ```
  Scenario: List drafts works without writes enabled
    Tool: interactive_bash + cassette
    Preconditions: TIKTOK_MCP_ALLOW_WRITES unset; cassette `posting_drafts/list.yaml`
    Steps:
      1. JSON-RPC list_pending_drafts with alias
      2. Assert response.result.drafts is a list (may be empty)
      3. Assert NO `writes_disabled` error
    Expected Result: read-only tool unaffected by gate
    Evidence: .omo/evidence/task-28-list-drafts.json

  Scenario: Publish a draft to PUBLIC
    Tool: interactive_bash + cassette
    Preconditions: env `TIKTOK_MCP_ALLOW_WRITES=posting`; cassette `posting_drafts/publish_public.yaml`
    Steps:
      1. JSON-RPC move_draft_to_publish with `publish_id: "draft-fixture-1"`, `post_info: {title: "QA test", privacy_level: "SELF_ONLY"}`
      2. Assert response.result.status in {"PROCESSING_UPLOAD","PUBLISH_COMPLETE"}
      3. Assert outbound body.post_info.privacy_level == "SELF_ONLY"
    Expected Result: draft transitions to publish queue
    Evidence: .omo/evidence/task-28-publish-draft.json
  ```

  **Commit**: `feat(posting): draft management writes + list_drafts tool` — `src/tiktok_mcp/tools/posting_writes_drafts.py`, tests, cassettes

- [x] 29. MCP Resources: `tiktok-mcp://accounts/` + `tiktok-mcp://app-credentials/`

  **What to do**:
  - `src/tiktok_mcp/resources/accounts.py` registers two MCP Resources via `@app.resource()`:
    - `tiktok-mcp://accounts/` → returns JSON list of all registered accounts: `[{alias, api_type, sandbox, has_valid_token, expires_at, last_used_at}]`. No secrets. Reuses `list_accounts` tool logic.
    - `tiktok-mcp://app-credentials/` → returns fingerprints of registered app credentials per API: `[{api_type, client_key_fingerprint, secret_set, sandbox_secret_set, registered_redirect_uri}]`.
  - Both Resources are read-only by MCP definition; no `destructiveHint`.
  - Resources update whenever the underlying keychain mutates (no caching — read keychain on each Resource fetch).

  **Must NOT do**:
  - Expose full client_key or any secret values (Decisions §1.2).
  - Cache the Resource response longer than the single fetch.
  - Auto-refresh expired tokens during a Resource read (Resources are read-only; user must call `refresh_token` tool explicitly).

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: thin wrapper around existing tools; mostly schema + decorator
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 with T30, T31, T32
  - **Blocks**: nothing
  - **Blocked By**: T10 (account tools — share logic), T11 (app credential tools)

  **References**:
  - MCP SDK Resources docs: `https://modelcontextprotocol.io/docs/concepts/resources`
  - FastMCP `@app.resource()` decorator pattern
  - Decisions of Record §6 (account model — alias/sandbox/has_valid_token fields)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_resources_accounts.py -k "test_accounts_resource_shape" → PASS`
  - [ ] `uv run pytest tests/unit/test_resources_accounts.py -k "test_no_secrets_in_credentials_resource" → PASS` (grep response for full client_key length match — must be fingerprinted)
  - [ ] `uv run pytest tests/integration/test_resources_e2e.py → PASS` (boots MCP, sends `resources/list` JSON-RPC, asserts both URIs present)

  **QA Scenarios**:

  ```
  Scenario: List accounts resource shows registered accounts
    Tool: interactive_bash (tmux) + JSON-RPC
    Preconditions: 2 accounts registered (`nordic-no-test`, `nordic-se-test`)
    Steps:
      1. Send `resources/read` JSON-RPC for `tiktok-mcp://accounts/`
      2. Assert response.contents[0].mimeType == "application/json"
      3. Assert JSON.parse(contents) has length 2
      4. Assert each entry has keys {alias, api_type, sandbox, has_valid_token, expires_at, last_used_at}
      5. Assert no keys contain "secret", "token", or "client_key" full values
    Expected Result: 2-entry account list with fingerprint-only data
    Evidence: .omo/evidence/task-29-accounts-resource.json

  Scenario: App credentials resource returns fingerprints only
    Tool: interactive_bash + JSON-RPC
    Preconditions: Display + Business app credentials registered
    Steps:
      1. Send `resources/read` for `tiktok-mcp://app-credentials/`
      2. Assert each entry's `client_key_fingerprint` matches pattern `^[A-Z0-9]{4}…[A-Z0-9]{4}$` (first-4 + ellipsis + last-4)
      3. Assert NO `client_secret` field present
      4. Assert `secret_set: true` boolean per entry
    Expected Result: fingerprint-only metadata
    Evidence: .omo/evidence/task-29-credentials-resource.json
  ```

  **Commit**: `feat(resources): accounts + app-credentials MCP resources` — `src/tiktok_mcp/resources/accounts.py`, `tests/unit/test_resources_accounts.py`, `tests/integration/test_resources_e2e.py`

- [x] 30. MCP Prompts: weekly report + comment queue templates

  **What to do**:
  - `src/tiktok_mcp/prompts/templates.py` registers 3 MCP Prompts via `@app.prompt()`:
    - `weekly_marketing_report(advertiser_alias: str, start_date: str, end_date: str)` → returns a prompt template instructing Claude to call `create_async_report` (T16), poll, download, then summarize key metrics in Norwegian + English with currency annotation. The Prompt is parameterized — Claude fills the alias and dates, then executes the suggested tool chain.
    - `comment_queue_triage(account_alias: str, video_id: str, max_comments: int = 50)` → returns prompt that has Claude call `list_comments` (T17), classify each into {SPAM, QUESTION, COMPLIMENT, NEGATIVE_FEEDBACK, OFF_TOPIC}, suggest reply for QUESTION/COMPLIMENT, and ask for confirmation before posting.
    - `weekly_engagement_summary(display_alias: str, days: int = 7)` → returns prompt that has Claude call `video_list` (T13), aggregate view/like/comment/share totals, identify top-3 videos by engagement rate.
  - Each Prompt is a `Prompt` object with `name`, `description`, `arguments[]`, and a `messages` function returning a list of `Message` objects.
  - All Prompts emphasize the **drafts-default** + **write-gating** rules in their template text so Claude doesn't surprise the user.

  **Must NOT do**:
  - Hard-code account aliases (parameterize via Prompt arguments).
  - Include API keys or tokens in any Prompt text.
  - Auto-execute the tool chain — Prompts are templates that *suggest* the chain; Claude decides when to execute.

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: bulk of work is the prompt text itself — clear, parameterized, user-facing language
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 with T29, T31, T32
  - **Blocks**: nothing
  - **Blocked By**: T13, T16, T17 (Prompts reference these tools by name)

  **References**:
  - MCP SDK Prompts docs: `https://modelcontextprotocol.io/docs/concepts/prompts`
  - FastMCP `@app.prompt()` decorator
  - Decisions of Record §4 (mention writes gate in Prompt text), §15 (mention currency pass-through in weekly report Prompt)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_prompts.py -k "test_three_prompts_registered" → PASS`
  - [ ] `uv run pytest tests/unit/test_prompts.py -k "test_prompts_mention_writes_gate" → PASS` (asserts "TIKTOK_MCP_ALLOW_WRITES" or "writes" appears in `comment_queue_triage`)
  - [ ] `uv run pytest tests/unit/test_prompts.py -k "test_prompts_have_required_args" → PASS` (parameters are surfaced via `arguments[]`)
  - [ ] `uv run pytest tests/integration/test_prompts_e2e.py → PASS` (`prompts/list` returns 3 entries)

  **QA Scenarios**:

  ```
  Scenario: Generate weekly_marketing_report prompt and verify text
    Tool: interactive_bash + JSON-RPC
    Preconditions: MCP running with default config
    Steps:
      1. JSON-RPC `prompts/get` with `name: "weekly_marketing_report"`, `arguments: {advertiser_alias: "nordic-no", start_date: "2026-05-15", end_date: "2026-05-22"}`
      2. Assert response.messages[0].content.text contains "create_async_report"
      3. Assert text contains both "NOK" or "currency" and the literal date range
      4. Assert text mentions `download_async_report`
    Expected Result: parameterized prompt with tool-chain instructions
    Evidence: .omo/evidence/task-30-weekly-report-prompt.json

  Scenario: comment_queue_triage Prompt mentions writes gate
    Tool: interactive_bash
    Preconditions: MCP running
    Steps:
      1. JSON-RPC `prompts/get` with `name: "comment_queue_triage"`
      2. Grep response.messages content for "TIKTOK_MCP_ALLOW_WRITES" or "writes enabled"
      3. Assert at least one match
    Expected Result: Prompt template self-documents the write-gate requirement
    Evidence: .omo/evidence/task-30-comment-triage-prompt.json
  ```

  **Commit**: `feat(prompts): weekly_marketing_report + comment_queue_triage + weekly_engagement_summary` — `src/tiktok_mcp/prompts/templates.py`, `tests/unit/test_prompts.py`, `tests/integration/test_prompts_e2e.py`

- [x] 31. stdio entry point + end-to-end MCP boot test

  **What to do**:
  - `src/tiktok_mcp/server.py` implements `def main() -> None: app.run(transport="stdio")`; ensures all tools/resources/prompts are imported (and thus registered) before `app.run()`.
  - `src/tiktok_mcp/__init__.py` exposes `from .server import main, app` plus `__version__` (read from hatch-vcs at runtime via `importlib.metadata`).
  - `--version` CLI flag handled in `main()` via `argparse` (exits 0 with version string before starting stdio loop). All other args ignored (stdio MCPs don't take args).
  - Boot must complete within 500ms (cold), 50ms (warm) on the CI matrix (3 OS × 3 Python).
  - `tests/integration/test_stdio_boot.py` subprocess-spawns `python -m tiktok_mcp`, writes `initialize` + `tools/list` + `resources/list` + `prompts/list` JSON-RPC frames over stdin, parses stdout, asserts:
    - `tools/list` returns expected count (sum of all `@app.tool()` registrations from T10-T19, T20-T28).
    - All write tools have `destructiveHint: true`.
    - All read tools have `readOnlyHint: true`.
    - No tool name collides with another (asserts uniqueness).
    - `resources/list` returns 2 (`accounts/`, `app-credentials/`).
    - `prompts/list` returns 3 (weekly_marketing_report, comment_queue_triage, weekly_engagement_summary).

  **Must NOT do**:
  - Write any data to stdout outside the MCP protocol frames (Decisions §17 — logs go to stderr only).
  - Block on keychain unlock during boot — defer keychain reads until first tool call.
  - Auto-register tools via dynamic import — explicit import in `server.py` keeps the dependency graph readable.

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: subprocess + JSON-RPC frame parsing + invariant assertions; orchestration-heavy
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 4 sequential after all tools/resources/prompts are registered
  - **Blocks**: T32 (README), T35 (release docs), F-Wave (all reviewers depend on tools/list output)
  - **Blocked By**: T10-T19, T20-T28, T29, T30 (all registrations must exist)

  **References**:
  - FastMCP stdio transport: `from mcp.server.fastmcp import FastMCP; app.run(transport="stdio")`
  - JSON-RPC frame format (Content-Length headers per LSP-style): `https://modelcontextprotocol.io/docs/concepts/transports`
  - Decisions §17 (stderr-only logging)

  **Acceptance Criteria**:
  - [ ] `uv run python -m tiktok_mcp --version` exits 0, prints version matching `git describe --tags --dirty`
  - [ ] `uv run pytest tests/integration/test_stdio_boot.py → PASS`
  - [ ] tools/list returns ≥40 tools (T10-T28 sum); exact count locked once Wave 3 lands
  - [ ] zero tools written to stdout (grep stdout for non-JSON-RPC text → empty)
  - [ ] boot time <500ms cold on Ubuntu CI runner (measured by `time` wrapper in CI)

  **QA Scenarios**:

  ```
  Scenario: Cold boot + tools/list + resources/list + prompts/list end-to-end
    Tool: Bash (Python subprocess)
    Preconditions: dependencies installed via `uv sync`; no env vars set
    Steps:
      1. Run `python tests/integration/test_stdio_boot.py --verbose` (or `pytest tests/integration/test_stdio_boot.py -v`)
      2. Capture stdout + stderr separately
      3. Parse stdout as sequence of JSON-RPC responses
      4. Assert `initialize` response has `serverInfo.name == "tiktok-mcp"`
      5. Assert all tools/list entries have either `readOnlyHint: true` or `destructiveHint: true` (not both, not neither)
      6. Assert tool name uniqueness (set comparison)
      7. Assert stderr does NOT contain any string from a known token allow-list (uses SecretRedactor)
    Expected Result: boot completes; all introspection frames return valid responses; no tokens leaked
    Evidence: .omo/evidence/task-31-stdio-boot.json (parsed frames) + .omo/evidence/task-31-stderr.log

  Scenario: --version flag exits cleanly
    Tool: Bash
    Preconditions: package installed via `uv pip install -e .`
    Steps:
      1. Run `tiktok-mcp --version`
      2. Assert exit code == 0
      3. Assert stdout matches regex `^tiktok-mcp \d+\.\d+\.\d+(?:[.-]\S+)?$`
      4. Assert no stdio MCP frames emitted
    Expected Result: clean version print, no server loop
    Evidence: .omo/evidence/task-31-version.txt
  ```

  **Commit**: `feat(server): stdio entry point + end-to-end boot test` — `src/tiktok_mcp/server.py`, `src/tiktok_mcp/__init__.py`, `src/tiktok_mcp/__main__.py`, `tests/integration/test_stdio_boot.py`

- [x] 32. README.md with claude_desktop_config.json examples (macOS + Windows + Linux)

  **What to do**:
  - `README.md` at project root containing:
    1. Tagline: "TikTok MCP — read your TikTok organic + ad performance, comments, and post content. Multi-account, multi-API, uvx-distributed."
    2. Quick-start: 3 commands max (`uvx tiktok-mcp@latest` smoke, then claude_desktop_config.json snippet, then "restart Claude Desktop").
    3. Supported APIs table: Display / Marketing / Business Organic / Content Posting with column "Read/Write/Both".
    4. claude_desktop_config.json examples for macOS (`~/Library/Application Support/Claude/claude_desktop_config.json`), Windows (`%APPDATA%\Claude\claude_desktop_config.json`), Linux (`~/.config/Claude/claude_desktop_config.json`) — each with exact JSON block including pinned version (`tiktok-mcp@0.1.0`) and env vars.
    5. First-time setup walkthrough: invoke `add_account` tool, paste back the redirect URL.
    6. Writes opt-in section: env-var values + per-API granularity table.
    7. Sandbox section: how to use `TIKTOK_MCP_USE_SANDBOX=1`.
    8. Security: keychain location per OS, encrypted-file fallback path (`platformdirs.user_data_dir`), redaction guarantees.
    9. Troubleshooting: locked keychain, expired state, mismatched redirect host (top 5 errors with copy-paste fix commands).
    10. License + contributing.
  - Word count target: 1500-3000 words.
  - All code examples are copy-paste-valid (run `npx markdownlint README.md` in CI to catch broken syntax).

  **Must NOT do**:
  - Include real app credentials, redirect URIs other than `https://oauth.example.com` placeholder, or actual account aliases.
  - Use marketing copy or emojis (Decisions: technical docs, no emojis).
  - Document deferred features (catalog manager, audience segments, etc.) — point to "Roadmap" section instead.

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: user-facing documentation, language-quality critical
  - **Skills**: none mandatory
  - **Skills Evaluated but Omitted**: `design-html` (not HTML output), `web-design-guidelines` (not web UI)

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 alongside T29, T30, T33, T34
  - **Blocks**: T37 (CI workflow references README in PR template)
  - **Blocked By**: T31 (tool count + names must be final to document accurately)

  **References**:
  - Decisions of Record (all sections — README cites §4, §5, §6, §7, §12, §16, §17)
  - `bg_8e111ce0` research: claude_desktop_config.json paths per OS + uvx pinning syntax
  - Existing TikTok MCP-style README precedents (not for plagiarism — for structure cues)

  **Acceptance Criteria**:
  - [ ] `wc -w README.md` between 1500 and 3000
  - [ ] `grep -c "claude_desktop_config.json" README.md` ≥ 3 (one per OS)
  - [ ] `grep "TIKTOK_MCP_ALLOW_WRITES" README.md` returns ≥1 line
  - [ ] All JSON blocks in README parse as valid JSON: `python tests/docs/validate_readme_json.py`
  - [ ] `npx --yes markdownlint-cli README.md` exits 0

  **QA Scenarios**:

  ```
  Scenario: Copy macOS config snippet → MCP boots in Claude Desktop sim
    Tool: Bash + Python subprocess (Claude Desktop not actually invoked; we simulate via raw stdio)
    Preconditions: package installable; README contains macOS JSON block
    Steps:
      1. Extract JSON between markdown code fences `claude_desktop_config_macos` from README
      2. Validate as JSON; assert `mcpServers["tiktok-mcp"]` exists
      3. Extract `command` + `args` from JSON
      4. Spawn subprocess with those exact args
      5. Send `initialize` JSON-RPC frame
      6. Assert response.serverInfo.name == "tiktok-mcp"
    Expected Result: README config example is executable
    Evidence: .omo/evidence/task-32-readme-macos-config.json

  Scenario: All embedded JSON blocks valid
    Tool: Bash
    Preconditions: README written
    Steps:
      1. `python tests/docs/validate_readme_json.py` (script extracts every ```json fence and json.loads each)
      2. Assert exit code 0
    Expected Result: zero broken JSON examples
    Evidence: .omo/evidence/task-32-readme-json-validation.txt
  ```

  **Commit**: `docs(readme): user-facing README with claude_desktop_config examples` — `README.md`, `tests/docs/validate_readme_json.py`

- [x] 33. `docs/auth-architecture.md` — OAuth + token storage design doc

  **What to do**:
  - `docs/auth-architecture.md` covering:
    1. Multi-account model (account = `(api_type, alias, sandbox_flag)` tuple).
    2. Manual-paste OAuth flow with sequence diagram (mermaid) showing: user invokes `add_account` → MCP generates state + PKCE → returns URL → user opens browser → user pastes redirect → MCP validates state → exchanges code → stores tokens.
    3. State manager design (in-memory dict + 10-min TTL + single-use; references T6).
    4. Token refresh strategy per API (Display: rotates on refresh; Business: longer-lived; Posting: rotates).
    5. Atomic refresh-token rotation (write new RT before discarding old; references T12).
    6. Per-account `asyncio.Lock` on refresh path (Decisions §11).
    7. Sandbox isolation (separate keychain namespace, `sandbox=true` tag, production tools refuse sandbox accounts; references Decisions §5).
    8. Recovery paths: locked keychain, expired state, expired refresh token, partial keychain write.
  - Use mermaid for 2-3 sequence diagrams (auth flow, refresh flow, sandbox isolation).
  - Word count: 800-2000 words.

  **Must NOT do**:
  - Document any localhost-callback flow (Decisions of Record §3).
  - Include actual token values or example secrets.
  - Speculate about deferred v0.2 features.

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: technical doc with diagrams; needs precision + clarity
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 with T29, T30, T32, T34, T35, T36
  - **Blocks**: nothing
  - **Blocked By**: T5, T6, T12 (must reference their actual implementations)

  **References**:
  - Decisions of Record §3 (auth flow), §5 (token storage), §11 (concurrency), §12 (atomic refresh)
  - `src/tiktok_mcp/auth/state_manager.py` (T6), `src/tiktok_mcp/auth/keychain.py` (T5)
  - Mermaid syntax: `https://mermaid.js.org/syntax/sequenceDiagram.html`

  **Acceptance Criteria**:
  - [ ] `wc -w docs/auth-architecture.md` between 800 and 2000
  - [ ] `grep -c "^\`\`\`mermaid" docs/auth-architecture.md` ≥ 2
  - [ ] `grep "localhost" docs/auth-architecture.md` returns 0 lines (or only in "what we explicitly DON'T do" subsection)
  - [ ] `python tests/docs/validate_mermaid.py docs/auth-architecture.md` exits 0 (mermaid blocks parse)

  **QA Scenarios**:

  ```
  Scenario: Mermaid diagrams render without errors
    Tool: Bash (mermaid-cli)
    Preconditions: `npm install -g @mermaid-js/mermaid-cli` available in CI
    Steps:
      1. Extract each ```mermaid block from docs/auth-architecture.md
      2. Pipe to `mmdc -i - -o /tmp/diagram-{N}.svg`
      3. Assert exit code 0; assert output svg file size > 0
    Expected Result: all diagrams render
    Evidence: .omo/evidence/task-33-mermaid-render.txt

  Scenario: Doc references match actual code symbols
    Tool: Bash + python
    Preconditions: code in T5/T6/T12 landed
    Steps:
      1. `python tests/docs/cross_ref.py docs/auth-architecture.md src/tiktok_mcp/`
      2. Script extracts all `src/...` paths and `ClassName.method_name` references from doc
      3. Asserts each path/symbol exists in source tree
    Expected Result: no broken references
    Evidence: .omo/evidence/task-33-cross-refs.txt
  ```

  **Commit**: `docs: auth architecture design doc` — `docs/auth-architecture.md`, `tests/docs/validate_mermaid.py`, `tests/docs/cross_ref.py`

- [x] 34. `docs/security-model.md` — secrets, redaction, PII, write gating

  **What to do**:
  - `docs/security-model.md` covering:
    1. Threat model: malicious agent prompt-injects writes; malicious account pastes phishing redirect URL; compromised app credentials; lost laptop with unlocked keychain.
    2. Defense layers:
       - Layer 1: OS keychain primary storage (`keyring` lib)
       - Layer 2: AES-fernet encrypted file fallback (`platformdirs.user_data_dir`)
       - Layer 3: SecretRedactor logging filter
       - Layer 4: httpx exception body sanitizer
       - Layer 5: `destructiveHint` annotations + Claude Desktop permission prompts
       - Layer 6: `TIKTOK_MCP_ALLOW_WRITES` env-var gate (re-evaluated per tool call)
       - Layer 7: per-API granularity (`=marketing,comments` etc.)
       - Layer 8: `TIKTOK_MCP_ALLOW_LIVE_WRITES` test-time gate (CI never sets it)
       - Layer 9: two-step `remove_account` with `confirmation_token` (60s TTL)
       - Layer 10: `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` gates onboarding tools separately from writes
    3. Threat-to-defense matrix (table).
    4. PII handling (comment text, audience uploads — never persist, never log at INFO).
    5. Sandbox isolation (separate keychain namespace).
    6. Known limitations: prompt-injection in Claude can still call tools; user is final gate.
    7. Reporting security issues: `[email protected]` (or GitHub Security Advisories link).
  - Word count: 1500-3000 words.

  **Must NOT do**:
  - Claim guarantees the MCP cannot deliver (e.g. "immune to prompt injection").
  - Include CVE-style stub language with no real CVE.
  - Reference any specific user's data.

  **Recommended Agent Profile**:
  - **Category**: `writing`
  - **Skills**: `cso` (security review skill — load to ensure threat modeling is comprehensive)
    - Reason: this doc is the security narrative; cso skill ensures threat coverage

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 with T29, T30, T32, T33, T35, T36
  - **Blocks**: nothing
  - **Blocked By**: T4 (SecretRedactor), T5 (keychain), T8 (write gate)

  **References**:
  - Decisions of Record §4, §5, §14 (redaction), §16 (PII), §19 (account-changes gate)
  - `src/tiktok_mcp/auth/redactor.py` (T4)
  - cso skill threat-modeling patterns

  **Acceptance Criteria**:
  - [ ] `wc -w docs/security-model.md` between 1500 and 3000
  - [ ] `grep -c "^| " docs/security-model.md` ≥ 10 (table rows in threat-to-defense matrix)
  - [ ] `grep -E "Layer [0-9]+:" docs/security-model.md` returns ≥ 10 lines
  - [ ] No "guarantees" or "immune" or "cannot be" claims (sanity grep)

  **QA Scenarios**:

  ```
  Scenario: Threat matrix has every threat mapped to ≥1 defense
    Tool: python
    Preconditions: doc written; matrix in standard format
    Steps:
      1. `python tests/docs/validate_security_matrix.py`
      2. Script parses the table, asserts each row's "Defenses" column is non-empty
      3. Asserts every "Layer N" referenced in Defenses column exists in Defense Layers section
    Expected Result: matrix is internally consistent
    Evidence: .omo/evidence/task-34-security-matrix.txt

  Scenario: No overclaim of immunity
    Tool: Bash
    Preconditions: doc written
    Steps:
      1. `grep -niE "(immune|guarantee|cannot be|impossible to)" docs/security-model.md`
      2. Assert zero matches OR only in clearly-hedged sentences ("we cannot guarantee...")
    Expected Result: no absolute claims
    Evidence: .omo/evidence/task-34-no-overclaim.txt
  ```

  **Commit**: `docs: security model + threat-to-defense matrix` — `docs/security-model.md`, `tests/docs/validate_security_matrix.py`

- [x] 35. `docs/release.md` — maintainer release runbook

  **What to do**:
  - `docs/release.md` covering:
    1. Versioning policy: SemVer, hatch-vcs tag-driven (no manual `__version__` edits).
    2. Pre-release checklist:
       - All F1-F4 reviewers APPROVE
       - CHANGELOG updated (manual edit or release-please PR merged)
       - Smoke test on local: `uv run pytest -q`, `uv run mypy src/`, `uv run ruff check src/`
       - Smoke test in TestPyPI: `git tag v0.1.0-rc.1`, push, wait for release-rc workflow, then `uvx --index-url https://test.pypi.org/simple/ tiktok-mcp@0.1.0-rc.1 --version`
    3. Production release steps:
       - Sanity: `git status` clean, `main` branch, all tests green
       - `git tag v0.1.0 -m "Release v0.1.0"`
       - `git push origin v0.1.0`
       - GitHub Actions runs `release.yml`: builds wheel + sdist, OIDC publishes to PyPI
       - Within 5 min: verify `https://pypi.org/project/tiktok-mcp/0.1.0/` returns 200
       - Smoke: `uvx tiktok-mcp@0.1.0 --version` on macOS + Linux + Windows VMs
    4. Hotfix flow: branch from tag, fix, tag `v0.1.1`, push.
    5. Rollback: PyPI does NOT support delete; cut new patch version with revert commit.
    6. PyPI pending-publisher bootstrap (one-time, from S2 spike result): exact 5-step click-through.
    7. CHANGELOG conventions: link to commits, Keep a Changelog format.

  **Must NOT do**:
  - Suggest `pip install --force-reinstall` as a hotfix (encourages bad ops habits).
  - Reference any private credentials or pre-shared secrets (OIDC is the only auth).

  **Recommended Agent Profile**:
  - **Category**: `writing`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 with T29, T30, T32-T34, T36
  - **Blocks**: T37, T38 (CI/release workflow can reference docs/release.md)
  - **Blocked By**: S2 (PyPI bootstrap spike)

  **References**:
  - S2 spike result (pending-publisher bootstrap)
  - `bg_8e111ce0` research: trusted publishing, hatch-vcs, release-please
  - https://docs.pypi.org/trusted-publishers/

  **Acceptance Criteria**:
  - [ ] `wc -w docs/release.md` between 800 and 2500
  - [ ] `grep "git tag" docs/release.md` ≥ 2 occurrences
  - [ ] `grep "OIDC" docs/release.md` ≥ 1 occurrence
  - [ ] All cited URLs return 200: `python tests/docs/check_links.py docs/release.md`

  **QA Scenarios**:

  ```
  Scenario: Dry-run rc release per docs
    Tool: Bash
    Preconditions: docs/release.md written; S2 spike completed; TestPyPI pending publisher registered
    Steps:
      1. Follow steps 1-5 of "Pre-release checklist" verbatim
      2. Tag `v0.0.0-rc.1` locally (do NOT push)
      3. `uv build` → assert wheel + sdist appear in dist/
      4. Inspect wheel metadata: `unzip -p dist/*.whl '*/METADATA' | grep -E "(Name|Version|Requires-Python)"`
      5. Assert Version matches `0.0.0rc1` (PEP 440 normalization)
    Expected Result: dry-run produces valid artifacts
    Evidence: .omo/evidence/task-35-dry-run-rc.txt

  Scenario: All external links reachable
    Tool: Bash
    Preconditions: docs/release.md written
    Steps:
      1. `python tests/docs/check_links.py docs/release.md`
      2. Assert exit code 0
    Expected Result: zero broken links
    Evidence: .omo/evidence/task-35-link-check.txt
  ```

  **Commit**: `docs: release runbook + hotfix flow` — `docs/release.md`, `tests/docs/check_links.py`

- [x] 36. Tool-name + annotation consistency audit (`destructiveHint`/`readOnlyHint`)

  **What to do**:
  - `tests/lint/test_tool_inventory.py` performs static-introspection assertions on all registered MCP tools:
    1. Every tool name matches regex `^[a-z][a-z0-9_]+$` (snake_case, no kebab-case, no PascalCase).
    2. Every tool has EXACTLY ONE of `readOnlyHint: true` or `destructiveHint: true` (never both, never neither).
    3. Every tool name uniqueness (no duplicate across modules).
    4. Every write tool name follows convention: starts with one of {`create_`, `update_`, `delete_`, `post_`, `move_`, `pin_`, `unpin_`, `hide_`, `unhide_`, `upload_`, `cancel_`, `revoke_`, `refresh_`}. Tools `add_account`, `complete_account_login`, `remove_account`, `rename_account`, `set_app_credentials` are ACCOUNT-CHANGE tools (per DoR §19), NOT writes per se — they follow rule 6b below. NOTE: `verify_app_credentials` and `list_app_credentials` are NOT in this list — they are read-only (rule 5).
    5. Every read tool name follows convention: starts with one of {`get_`, `list_`, `describe_`, `search_`, `verify_`}. NOTE: `refresh_` and `revoke_` are DESTRUCTIVE (they mutate token state — refresh rotates the refresh token in keychain; revoke invalidates TikTok-side authorization) and therefore live under rule 4, NOT here. `verify_` IS in the read list (it does no mutation; the verify_app_credentials tool returns an ephemeral verification result via `AppCredentialsVerifyResult` without persisting).
    6. Every data-write tool (per rule 4 except account-change tools) is decorated with `@require_writes_enabled("<api>")` (introspect via `inspect.getsource` or attribute marker set by the decorator).
    6b. Every account-change tool (`add_account`, `complete_account_login`, `remove_account`, `rename_account`, `set_app_credentials`) is decorated with `@require_account_changes_enabled` per DoR §19 — this is the explicit, documented exception to rule 6, justified by user-permission orthogonality (onboarding vs. data-mutation lifecycles). Note: `verify_app_credentials` and `list_app_credentials` are READ-ONLY and NOT gated by `@require_account_changes_enabled` — they only inspect existing keychain entries without mutation.
    7. Every write tool has at least 1 QA scenario in the plan with `Expected Result` mentioning "writes_disabled" (cross-check via plan parser).
    8. `tools/list` returns >=40 entries (locked once T31 records final count).
  - `src/tiktok_mcp/internal/tool_registry.py` provides `list_all_tools_with_annotations() → list[dict]` helper used by both the lint test and the F1 reviewer.

  **Must NOT do**:
  - Allow `verify_` (no mutation) to be classified as writes. `refresh_` and `revoke_` ARE writes (they mutate token state) and MUST be classified as such — this reverses an earlier (incorrect) note.
  - Add new conventions without updating this lint test.
  - Make this test optional in CI.

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: structural lint; small surface area
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES (after all tools registered)
  - **Parallel Group**: Wave 4 with T29, T30, T32-T35
  - **Blocks**: F1 (reviewer uses `list_all_tools_with_annotations`)
  - **Blocked By**: T10-T28 (every tool must exist)

  **References**:
  - Decisions of Record §4 (annotations)
  - T8 (write-gate decorator marker)
  - T31 (tool count)

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/lint/test_tool_inventory.py → PASS`
  - [ ] If a future tool violates naming, test fails with clear `AssertionError: tool 'fooBar' violates snake_case`
  - [ ] If a write tool is missing `@require_writes_enabled`, test fails with `AssertionError: tool 'delete_X' missing write-gate decorator`
  - [ ] If two tools share a name, test fails with `AssertionError: duplicate tool name 'list_videos'`

  **QA Scenarios**:

  ```
  Scenario: Audit catches a missing destructiveHint annotation
    Tool: pytest
    Preconditions: artificially create a write tool without annotation in fixture module
    Steps:
      1. Add `tests/fixtures/bad_tool.py` with `@app.tool() def create_bad(): pass` (no destructiveHint)
      2. Run `pytest tests/lint/test_tool_inventory.py -v`
      3. Assert test fails with message containing "create_bad" and "destructiveHint"
      4. Remove fixture; rerun; assert PASS
    Expected Result: lint correctly rejects bad tools
    Evidence: .omo/evidence/task-36-lint-bad-tool.txt

  Scenario: Inventory output matches plan expectations
    Tool: pytest
    Preconditions: all tools registered
    Steps:
      1. Run `python -c "from tiktok_mcp.internal.tool_registry import list_all_tools_with_annotations; import json; print(json.dumps(list_all_tools_with_annotations(), indent=2))"`
      2. Save to `.omo/evidence/task-36-tool-inventory.json`
      3. Assert count >= 40
      4. Assert count of `destructiveHint: true` matches sum of write tools listed in plan (T20-T28)
    Expected Result: inventory matches plan
    Evidence: .omo/evidence/task-36-tool-inventory.json
  ```

  **Commit**: `test(lint): tool-name + annotation inventory audit` — `tests/lint/test_tool_inventory.py`, `src/tiktok_mcp/internal/tool_registry.py`

- [x] 37. `.github/workflows/ci.yml` — lint + type + test matrix

  **What to do**:
  - `.github/workflows/ci.yml` triggered on `push` to any branch + `pull_request` to `main`. Jobs:
    1. **lint**: ubuntu-latest only. Runs `ruff check src/ tests/`, `ruff format --check`, `mypy --strict src/`. Caches uv venv.
    2. **test**: matrix `os: [ubuntu-latest, macos-latest, windows-latest]` × `python-version: ["3.11", "3.12", "3.13"]` = 9 cells. Each cell: install uv, `uv sync --all-extras`, `uv run pytest tests/unit/ tests/integration/ -q --tb=short` with `TIKTOK_MCP_ALLOW_LIVE_WRITES` UNSET (CI never sets it). Cassettes cover replay paths.
    3. **smoke**: matrix `os: [ubuntu-latest, macos-latest, windows-latest]`. After test job passes, builds wheel + sdist via `uv build`, installs into a fresh venv via `uvx --from ./dist/*.whl tiktok-mcp --version`, asserts exit 0 + version string. Validates `claude_desktop_config.json` example from README via `python tests/docs/validate_readme_json.py`.
    4. **docs**: ubuntu-latest. Runs `python tests/docs/validate_mermaid.py docs/`, `npx --yes markdownlint-cli README.md docs/`, `python tests/docs/check_links.py README.md docs/release.md`.
  - All jobs use `actions/checkout@v6` with `fetch-depth: 0` (required for hatch-vcs).
  - All jobs use `astral-sh/setup-uv@v5` with `enable-cache: true`.
  - `concurrency: { group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true }`.
  - Status badge added to README.

  **Must NOT do**:
  - Set `TIKTOK_MCP_ALLOW_LIVE_WRITES=1` in CI (Decisions §4).
  - Run real-network integration tests against TikTok's production APIs (cassette-only).
  - Cache `~/.config/keyring` or any keychain data.
  - Use deprecated `actions/setup-python@v4` (use v6+ for proper caching).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: CI YAML is config-but-load-bearing; matrix correctness + caching subtleties
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 with T38, T39, T40, T41
  - **Blocks**: T42 (production release waits on green CI)
  - **Blocked By**: T31 (boot test exists), T36 (lint test exists)

  **References**:
  - `bg_8e111ce0` research notes on uv setup actions and matrix caching
  - https://docs.astral.sh/uv/guides/integration/github/
  - https://github.com/actions/setup-python

  **Acceptance Criteria**:
  - [ ] First push to a PR triggers all 4 jobs
  - [ ] All 9 test-matrix cells pass on first green build
  - [ ] `gh workflow view ci.yml --json conclusion` returns `"success"` after green build
  - [ ] `actions/checkout@v6` + `fetch-depth: 0` present in all jobs (grep)
  - [ ] No `TIKTOK_MCP_ALLOW_LIVE_WRITES` references anywhere in workflows

  **QA Scenarios**:

  ```
  Scenario: Push a no-op commit → CI runs all 4 jobs to green
    Tool: Bash + gh CLI
    Preconditions: branch `ci-validation`; ci.yml landed
    Steps:
      1. `git commit --allow-empty -m "ci: smoke validation"`
      2. `git push origin ci-validation`
      3. `gh run watch` (wait up to 15 min for completion)
      4. `gh run view --json conclusion -q .conclusion` → assert "success"
      5. `gh run view --json jobs -q '.jobs | length'` → assert ≥ 13 (4 lint + 9 test cells via matrix expansion)
    Expected Result: all jobs green
    Evidence: .omo/evidence/task-37-ci-green.txt (gh run view output)

  Scenario: Matrix expansion includes 3 OS × 3 Python
    Tool: Bash + gh CLI
    Preconditions: CI run completed
    Steps:
      1. `gh run view <run-id> --json jobs -q '.jobs[] | select(.name | startswith("test")) | .name'`
      2. Assert output includes `test (ubuntu-latest, 3.11)`, `test (windows-latest, 3.13)`, `test (macos-latest, 3.12)`, plus 6 others
    Expected Result: 9 distinct test cells
    Evidence: .omo/evidence/task-37-matrix-jobs.txt
  ```

  **Commit**: `ci: lint + type + 9-cell test matrix + smoke + docs jobs` — `.github/workflows/ci.yml`, README status badge update

- [x] 38. `.github/workflows/release.yml` — tag-triggered OIDC PyPI publish

  **What to do**:
  - `.github/workflows/release.yml` triggered ONLY on `push` of tags matching `v[0-9]+.[0-9]+.[0-9]+*`. Single job `release`:
    - `runs-on: ubuntu-latest`
    - `permissions: { id-token: write, contents: read }` (JOB-level, not workflow-level — security best practice)
    - `environment: { name: pypi, url: https://pypi.org/p/tiktok-mcp }` (uses the GitHub Environment named `pypi`)
    - Steps:
      1. `actions/checkout@v6` with `fetch-depth: 0` (hatch-vcs needs full history for version)
      2. `astral-sh/setup-uv@v5` with cache
      3. `uv build` (produces wheel + sdist in `dist/`)
      4. Smoke install: `uv run --with ./dist/*.whl python -c "import tiktok_mcp; print(tiktok_mcp.__version__)"`
      5. `pypa/gh-action-pypi-publish@release/v1` with `verify-metadata: true`, `print-hash: true` — defaults to PyPI prod via OIDC
      6. Post-publish: poll `https://pypi.org/pypi/tiktok-mcp/<version>/json` until 200 (max 5 min)
      7. Post-publish smoke: `uvx tiktok-mcp@<version> --version` in fresh tmpdir → assert exit 0
      8. Create GitHub Release via `softprops/action-gh-release@v2` with auto-generated notes (uses release-please output if T40 picked release-please, else `gh-release` autogen)
  - Pre-release tags (`v0.1.0-rc.1`, `v0.1.0a1`, etc.) route to TestPyPI instead (parallel `release-testpypi` job triggered on tags matching `v*-*` or `v*[abc]*`).

  **Must NOT do**:
  - Use API tokens or username/password (OIDC only).
  - Set workflow-level `id-token: write` (job-level scoping is the security best practice).
  - Skip the post-publish smoke (catches half-published / metadata-broken packages).
  - Publish without a tag matching the SemVer pattern (filter regex enforces).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: load-bearing release infra; getting OIDC permissions wrong has security + supply-chain consequences
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 with T37, T39, T40, T41
  - **Blocks**: T42 (production release)
  - **Blocked By**: T1 (pyproject + hatch-vcs), T39 (pending-publisher registered)

  **References**:
  - `bg_8e111ce0` research notes
  - https://github.com/pypa/gh-action-pypi-publish
  - https://docs.pypi.org/trusted-publishers/
  - https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect

  **Acceptance Criteria**:
  - [ ] `release.yml` triggers only on `v*` tags (regex check)
  - [ ] `id-token: write` is JOB-level only (grep workflow vs job blocks)
  - [ ] `environment: pypi` referenced exactly once
  - [ ] `fetch-depth: 0` set on checkout step
  - [ ] No `password:`, `username:`, `PYPI_API_TOKEN`, or `secrets.PYPI*` references anywhere

  **QA Scenarios**:

  ```
  Scenario: Push v0.0.0-rc.1 tag → release-testpypi job runs and publishes
    Tool: Bash + gh CLI
    Preconditions: T39 pending-publisher registered for TestPyPI; release.yml landed
    Steps:
      1. `git tag v0.0.0-rc.1 -m "Release smoke RC"`
      2. `git push origin v0.0.0-rc.1`
      3. `gh run watch` → assert completion success
      4. `curl -fsSL https://test.pypi.org/pypi/tiktok-mcp/0.0.0rc1/json | jq -r .info.version` → assert "0.0.0rc1"
      5. `uvx --index-url https://test.pypi.org/simple/ tiktok-mcp@0.0.0rc1 --version` → assert exit 0
    Expected Result: rc artifact published + installable via uvx from TestPyPI
    Evidence: .omo/evidence/task-38-rc-testpypi-publish.txt

  Scenario: Production PyPI routing — STATIC workflow assertion only (NO live prod publish)
    Tool: Bash (yq + actionlint)
    Preconditions: release.yml landed; rc validation passed
    Steps:
      1. Parse the workflow YAML: `yq '.jobs.publish.steps[] | select(.uses=="pypa/gh-action-pypi-publish@release/v1") | .with' .github/workflows/release.yml`
      2. Assert it contains BOTH a TestPyPI-routed step (`repository-url: https://test.pypi.org/legacy/`) AND a prod-routed step (no `repository-url`, defaulting to PyPI)
      3. Assert prod step has a conditional `if: ${{ !contains(github.ref_name, 'rc') && !contains(github.ref_name, 'alpha') && !contains(github.ref_name, 'beta') }}` (only non-prerelease tags go to prod)
      4. Assert TestPyPI step has the inverse `if` condition
      5. Run `actionlint .github/workflows/release.yml` → exit 0
    Expected Result: workflow YAML correctly routes by tag shape; no live prod publish occurs in this scenario (production PyPI live-publish smoke is deferred to T42)
    Evidence: .omo/evidence/task-38-prod-routing-static.txt

  > **Note on production publish in T38**: We INTENTIONALLY do not push a non-rc tag during T38's development. The first non-rc tag is `v0.1.0` pushed during T42 — that's the only live production publish of the v0.1.0 release. T38's job here is to PROVE the workflow file would correctly route a non-rc tag, by static YAML assertions, not by actually performing the publish.

  Scenario: Push non-tag commit → release workflow does NOT run
    Tool: Bash + gh CLI
    Preconditions: release.yml landed
    Steps:
      1. `git commit --allow-empty -m "no-op"`
      2. `git push origin main`
      3. `gh run list --workflow=release.yml --json status,headBranch` → assert no run triggered by this commit
    Expected Result: release workflow correctly scoped to tags
    Evidence: .omo/evidence/task-38-no-trigger-on-commit.txt
  ```

  **Commit**: `ci(release): OIDC PyPI publish on v* tags (job-level id-token, env: pypi)` — `.github/workflows/release.yml`

- [x] 39. PyPI + TestPyPI pending-publisher registration walkthrough

  **What to do**:
  - `docs/pending-publisher-bootstrap.md` — copy-paste runbook for first-time setup:
    1. **TestPyPI registration** (do first, before any tag push):
       - Navigate to `https://test.pypi.org/manage/account/publishing/`
       - Click "Add pending publisher"
       - Project name: `tiktok-mcp`
       - Owner: `<github-org-or-username>`
       - Repository name: `tiktok-mcp`
       - Workflow name: `release.yml`
       - Environment name: `pypi`
       - Save.
    2. **GitHub Environment `pypi` creation**:
       - Repo Settings → Environments → New environment `pypi`
       - Optionally: required reviewer + branch protection (only `main` branch can deploy to `pypi`)
       - Save.
    3. **Production PyPI registration** (after rc validation succeeded):
       - Same form at `https://pypi.org/manage/account/publishing/`
       - Identical project + owner + repo + workflow + environment values.
    4. **Verification commands**:
       - `gh api /repos/<org>/<repo>/environments/pypi` returns 200
       - First successful TestPyPI publish completes the "pending → active" transition
  - Include screenshots in `docs/images/pending-publisher-*.png` (capture during S2 spike if possible).
  - Cross-link from `docs/release.md` and README.

  **Must NOT do**:
  - Embed any API token or secret in the doc (OIDC has none).
  - Skip the TestPyPI step ("prod-first" risks burning the project name on a half-published artifact).
  - Recommend `pypi-uploader` (deprecated path).

  **Recommended Agent Profile**:
  - **Category**: `writing`
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 with T37, T38, T40, T41
  - **Blocks**: T42 (the first live production publish — v0.1.0 — only happens in T42 after F1-F4 APPROVE, and requires the production pending-publisher form to have been completed by then). Note: T38 does NOT perform live production publish; T38's production routing is verified statically in T38 QA via yq/actionlint only.
  - **Blocked By**: S2 spike (validates the bootstrap flow conceptually)

  **References**:
  - S2 spike findings
  - https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
  - https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment

  **Acceptance Criteria**:
  - [ ] `wc -w docs/pending-publisher-bootstrap.md` between 400 and 1500
  - [ ] All step numbers monotonic; no skipped numbers
  - [ ] References to TestPyPI form URL and Prod PyPI form URL both present
  - [ ] `python tests/docs/check_links.py docs/pending-publisher-bootstrap.md` exits 0

  **QA Scenarios**:

  ```
  Scenario: TestPyPI registration produces successful first publish
    Tool: Bash (manual operator step + automated verification)
    Preconditions: operator has followed steps 1-2 in the doc
    Steps:
      1. Run: `curl -fsSL "https://test.pypi.org/pypi/tiktok-mcp/0.0.0rc1/json" | jq -e '.info.version == "0.0.0rc1"'` (verifies the rc actually published, which only happens AFTER the pending-publisher form was submitted + first publish completed the binding)
      2. Run: `curl -fsSL "https://test.pypi.org/pypi/tiktok-mcp/0.0.0rc1/json" | jq -r '.urls[0].url'` → assert URL contains "/files.pythonhosted.org/" (binding-verified artifact)
      3. Run: `python -c "import json, urllib.request; data = json.loads(urllib.request.urlopen('https://test.pypi.org/pypi/tiktok-mcp/0.0.0rc1/json').read()); assert 'tiktok-mcp' in data['info']['name']"`

    > **Operator one-time external step (per AGENTS.md operator-unblock-checklist convention):** filling out the PyPI pending-publisher form is operator-only because it requires logging into pypi.org. This step is NOT part of automated acceptance criteria. It is captured in `.sisyphus/evidence/operator-unblock-checklist.md` as a prerequisite to running the above automated checks. The above QA verifies the OUTCOME of that operator step (rc was published, binding is active), not the operator action itself.
    Expected Result: pending → active transition occurred on first publish
    Evidence: .omo/evidence/task-39-testpypi-bootstrap.txt

  Scenario: Production PyPI bootstrap config is symmetric with TestPyPI (static check)
    Tool: Bash (yq + grep)
    Preconditions: operator has filled out BOTH the TestPyPI and production PyPI pending-publisher forms per docs/pending-publisher-bootstrap.md steps 1-3 (operator-only one-time external step, captured in .sisyphus/evidence/operator-unblock-checklist.md). No live production publish occurs in this scenario — T38 has been intentionally restructured to not push a non-rc tag during development, and the first live production publish happens in T42 only after F1-F4 APPROVE.
    Steps:
      1. Run: `yq '.jobs.publish-production' .github/workflows/release.yml` → assert non-empty
      2. Run: `yq '.jobs.publish-production.permissions.id-token' .github/workflows/release.yml` → assert == "write"
      3. Run: `yq '.jobs.publish-production.environment.name' .github/workflows/release.yml` → assert == "pypi" (matches the GitHub environment name the operator registered in the production pending-publisher form)
      4. Run: `yq '.jobs.publish-production.steps[] | select(.uses // "" | test("pypa/gh-action-pypi-publish"))' .github/workflows/release.yml` → assert exactly one match exists (the action that consumes the OIDC binding); assert no `password:` or `username:` field is set (must be OIDC, not token-based)
      5. Run: `yq '.jobs.publish-production.steps[] | select(.uses // "" | test("pypa/gh-action-pypi-publish")) | .with."repository-url" // ""' .github/workflows/release.yml` → assert empty (PyPI default = production, asymmetry vs TestPyPI which sets `repository-url: https://test.pypi.org/legacy/`); confirms prod job points at prod PyPI
      6. Run: `gh api /repos/<org>/<repo>/environments/pypi --jq '.protection_rules // []'` → assert returns 200 and the environment exists (the operator created it as part of the pending-publisher form). Skip with note if `<org>/<repo>` isn't substituted yet at this stage of development.
      7. Confirm the rc binding from T38 is independent of the prod binding: this scenario does NOT perform a prod publish and does NOT verify a published prod artifact. Production-PyPI live verification — including `curl https://pypi.org/pypi/tiktok-mcp/0.1.0/json` and metadata symmetry with TestPyPI — happens in T42 only.
    Expected Result: production publish job config is structurally symmetric with the rc publish job (both OIDC, both pointing at their respective PyPI instance), the operator has completed the prod pending-publisher form (verified via existence of the `pypi` GitHub environment), and no live publish was triggered.
    Failure Indicators:
      - yq returns null for any of the assertions → workflow malformed; reject
      - `password:` or `username:` field present in the production publish step → token-based auth still configured; reject (would defeat OIDC)
      - Production publish job points at TestPyPI URL → asymmetry; reject
      - GitHub environment `pypi` does not exist → operator step 3 not completed; mark as operator-unblock (NOT a config defect)
    Evidence: .omo/evidence/task-39-prod-bootstrap-static.txt
  ```

  **Commit**: `docs: PyPI + TestPyPI pending-publisher bootstrap runbook` — `docs/pending-publisher-bootstrap.md`, `docs/images/pending-publisher-*.png`

- [x] 40. CHANGELOG automation via release-please

  **What to do**:
  - `.github/workflows/release-please.yml` configured to run on push to `main` only. Uses `googleapis/release-please-action@v4`.
  - `.release-please-manifest.json` (initial: `{".": "0.0.0"}`).
  - `release-please-config.json` declares:
    - `release-type: python`
    - `include-component-in-tag: false`
    - `bump-minor-pre-major: true`
    - `changelog-sections`: feat → Features, fix → Bug Fixes, ci/build → Infrastructure, docs → Documentation, refactor/perf → Internal, test → Tests, chore → Chores
    - `extra-files`: empty (hatch-vcs handles version)
  - The release-please bot opens/maintains a "release PR" on `main` that accumulates Conventional Commit messages → updated `CHANGELOG.md` + bumped version in manifest.
  - When the release PR is merged, release-please pushes the corresponding `vX.Y.Z` git tag, which triggers `release.yml` (T38).
  - `CHANGELOG.md` follows Keep a Changelog 1.1.0 format; release-please generates entries from PR/commit subjects.

  **Must NOT do**:
  - Manually edit `CHANGELOG.md` between releases (release-please owns it; manual edits get clobbered).
  - Bypass the release PR by hand-tagging (breaks the changelog state).
  - Use `semantic-release` (heavier, less PR-driven, harder to review).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: release automation config; getting commit-type → section mapping wrong is annoying to fix in flight
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 with T37, T38, T39, T41
  - **Blocks**: T42 production release uses the release PR flow
  - **Blocked By**: T38 (release.yml exists to be triggered by release-please's tags)

  **References**:
  - https://github.com/googleapis/release-please
  - `bg_8e111ce0` research: release-please vs semantic-release trade-off
  - https://keepachangelog.com/en/1.1.0/

  **Acceptance Criteria**:
  - [ ] `.github/workflows/release-please.yml` triggers on `push: { branches: [main] }`
  - [ ] First commit after merge produces a release-please PR titled `chore(main): release X.Y.Z`
  - [ ] `release-please-config.json` parses as valid JSON
  - [ ] `release-please-manifest.json` parses as valid JSON
  - [ ] `CHANGELOG.md` exists (empty body initially, release-please populates on first release)

  **QA Scenarios**:

  ```
  Scenario: First commit on main produces a release PR
    Tool: Bash + gh CLI
    Preconditions: release-please workflow landed; baseline `0.0.0` in manifest
    Steps:
      1. Land any `feat:` commit on `main` (e.g. `feat(tools): seed commit for release automation`)
      2. Wait for release-please workflow run (≤2 min)
      3. `gh pr list --label "autorelease: pending" --json number,title`
      4. Assert at least one PR exists with title matching `chore(main): release \d+\.\d+\.\d+`
    Expected Result: release PR appears
    Evidence: .omo/evidence/task-40-release-pr-created.txt

  Scenario: Merging the release PR fires release.yml via tag push
    Tool: Bash + gh CLI
    Preconditions: release PR exists; T38 release.yml landed
    Steps:
      1. `gh pr merge <release-pr-number> --squash`
      2. Wait for `release-please` follow-up workflow to push the tag
      3. `gh run list --workflow=release.yml --limit=1` → assert `event: push`, `head_branch` matches the tag
      4. `gh run view <id>` → assert success
    Expected Result: end-to-end main-merge → tag → publish chain
    Evidence: .omo/evidence/task-40-tag-pushed.txt
  ```

  **Commit**: `ci: release-please CHANGELOG automation + release PR flow` — `.github/workflows/release-please.yml`, `release-please-config.json`, `release-please-manifest.json`, `CHANGELOG.md`

- [x] 41. Distribution smoke matrix (uvx + claude_desktop_config) across 3 OS × 3 Python

  **What to do**:
  - `.github/workflows/distribution-smoke.yml` runs **after** a successful `release.yml` (uses `workflow_run` trigger on `release.yml` completion + `conclusion: success`). Job matrix: `os: [ubuntu-latest, macos-latest, windows-latest]` × `python-version: ["3.11", "3.12", "3.13"]`.
  - Each cell:
    1. Fresh runner; no pre-installed Python in the venv path.
    2. Install uv: `astral-sh/setup-uv@v5`.
    3. Wait up to 5 min for PyPI propagation (loop `curl -fsSL pypi.org/pypi/tiktok-mcp/<version>/json` until 200).
    4. `uvx tiktok-mcp@<version> --version` → assert exit 0 + version string.
    5. Spawn the MCP via subprocess with the exact `claude_desktop_config.json` snippet from the README; send `initialize` + `tools/list` JSON-RPC frames; assert `tools/list` count matches the locked count from T31.
    6. Per-OS: assert keychain backend boots without prompts on a non-interactive runner (use `keyring.backends.fail.Keyring` as documented fallback for CI; the in-CI test does NOT exercise real keychain).
  - Failures DO NOT yank the release (PyPI doesn't allow yank-by-CI), but they:
    - Open a GitHub issue tagged `release-blocker` with the failing cell + run URL
    - Comment on the most recent release with `⚠️ Distribution smoke failed on <cell>`
    - Set the workflow status to red so the maintainer sees it on the repo page

  **Must NOT do**:
  - Trigger before `release.yml` succeeds (use `workflow_run`).
  - Set the test gate `TIKTOK_MCP_ALLOW_LIVE_WRITES` in this workflow.
  - Use cached venvs across runs (the entire point is fresh-install validation).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: cross-platform test surface + asynchronous PyPI propagation + structured failure handling
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 with T37-T40
  - **Blocks**: nothing (informational signal)
  - **Blocked By**: T38 (release.yml exists)

  **References**:
  - https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run
  - `bg_8e111ce0` research notes on uvx + claude_desktop_config wiring per OS

  **Acceptance Criteria** (T41 runs BEFORE T42 — workflow + TestPyPI-rc smoke only; v0.1.0 prod smoke moves to T42):
  - [ ] `actionlint .github/workflows/distribution-smoke.yml` → exit 0
  - [ ] `yq '.jobs.smoke.strategy.matrix | (.os | length) * (.python | length)' .github/workflows/distribution-smoke.yml` → assert == 9 (3 OS × 3 Python)
  - [ ] `yq '.on' .github/workflows/distribution-smoke.yml` triggers on `workflow_run` of `release.yml` AND on `workflow_dispatch` (manual trigger for ad-hoc smoke)
  - [ ] Manually-triggered TestPyPI-rc smoke run (`gh workflow run distribution-smoke.yml -f source=testpypi -f version=0.0.0rc1`) returns success on all 9 cells
  - [ ] On simulated failure (fixture branch), workflow opens issue + comments on release (TestPyPI failure path validated)
  - [ ] No `TIKTOK_MCP_ALLOW_LIVE_WRITES` references in this workflow

  **QA Scenarios** (all agent-executable; NO dependency on T42 having published anything):

  ```
  Scenario: distribution-smoke.yml workflow is syntactically valid + matrix-shaped correctly
    Tool: Bash (actionlint + yq)
    Preconditions: T41 complete; .github/workflows/distribution-smoke.yml exists
    Steps:
      1. `actionlint .github/workflows/distribution-smoke.yml`
      2. `yq '.jobs.smoke.strategy.matrix.os | length' .github/workflows/distribution-smoke.yml` → assert 3
      3. `yq '.jobs.smoke.strategy.matrix.python | length' .github/workflows/distribution-smoke.yml` → assert 3
      4. `yq '.on.workflow_run.workflows[0]' .github/workflows/distribution-smoke.yml` → assert "release.yml"
    Expected Result: all 4 checks pass
    Evidence: .omo/evidence/task-41-workflow-static-validation.txt

  Scenario: Manually-triggered smoke against TestPyPI rc returns success on all 9 cells
    Tool: Bash + gh CLI
    Preconditions: T38 published an rc to TestPyPI (per T38 QA Scenario 1); distribution-smoke.yml landed
    Steps:
      1. `gh workflow run distribution-smoke.yml -f source=testpypi -f version=0.0.0rc1`
      2. Wait up to 20 min for completion: `gh run watch --exit-status`
      3. `gh run view --json jobs -q '.jobs | length'` → assert ≥ 9
      4. `gh run view --json conclusion -q .conclusion` → assert "success"
    Expected Result: all 9 cells green against TestPyPI rc; proves the smoke harness works BEFORE v0.1.0 prod release
    Evidence: .omo/evidence/task-41-testpypi-rc-smoke.txt

  Scenario: Simulated PyPI propagation delay handled by retry loop
    Tool: Bash (local test of the wait loop script)
    Preconditions: extract wait-loop logic to standalone script `.github/scripts/wait_for_pypi.sh`
    Steps:
      1. Mock pypi.org via a local nginx returning 404 then 200 after 60s
      2. Run the script against the mock with 5-min timeout
      3. Assert exit 0 with elapsed time between 60-90s
    Expected Result: script tolerates real-world propagation timing
    Evidence: .omo/evidence/task-41-wait-loop.txt
  ```

  **Commit**: `ci(smoke): post-release distribution validation across 9 cells` — `.github/workflows/distribution-smoke.yml`, `.github/scripts/wait_for_pypi.sh`

- [~] 42. v0.1.0 production release (tag + publish + verify) → BLOCKED EXTERNAL — see .omo/evidence/operator-unblock-checklist.md (live release ceremony — requires remote GitHub repo with `pypi` Environment configured + completed PyPI pending-publisher form)

  > **HARD GATE: Do NOT execute this task until ALL of F1, F2, F3, F4 have returned `VERDICT: APPROVE`.** The Final Verification Wave runs BEFORE the release tag. Per AGENTS.md, once all reviewers APPROVE, the agent ticks F1-F4 boxes → THEN executes T42 → THEN pushes v0.1.0 tag. This is the only correct ordering. If any reviewer returns REJECT, fix the cited issues, rerun the reviewer, and only when its verdict flips to APPROVE may T42 proceed.

  **What to do**:
  - This is the **terminal task** that ships v0.1.0 to PyPI. Executed only after:
    - All prior tasks (T1-T41) marked complete with green QA evidence
    - F1-F4 reviewers have run on the codebase + plan and all returned APPROVE (see Final Verification Wave below)
  - Steps:
    1. Sanity sweep: `git status` clean, on `main` branch, all CI green on latest commit
    2. Confirm CHANGELOG.md entries cover all delivered tasks (release-please should have populated)
    3. Merge the open release-please PR (which proposes `0.1.0`)
    4. release-please pushes `v0.1.0` tag automatically; `release.yml` (T38) fires
    5. Watch `gh run watch` until success
    6. Verify `https://pypi.org/project/tiktok-mcp/0.1.0/` returns 200 within 5 min
    7. Verify `uvx tiktok-mcp@0.1.0 --version` from a fresh tmpdir → 0.1.0
    8. Trigger `distribution-smoke.yml` (T41) auto-fires; watch it
    9. Smoke `claude_desktop_config.json` in a real Claude Desktop install (OPERATOR-REQUIRED external step — see operator-unblock-checklist; not an automated acceptance criterion)
    10. Post-release: announce in repo README (badge auto-updates), close `release-blocker` issues (none expected)

  **Must NOT do**:
  - Hand-tag `v0.1.0` outside release-please (breaks the changelog state).
  - Publish to prod before TestPyPI smoke (T38 + T41) passed at least once with an rc.
  - Skip the operator post-release Claude Desktop boot check — that is the only manual step in the release flow and it lives outside automated acceptance criteria (it is captured by the operator-unblock-checklist mechanism per AGENTS.md, not by an in-task acceptance criterion).

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: ceremonial release task with multi-step verification; needs careful sequencing
  - **Skills**: none mandatory

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: terminal (sole task in final release wave, executed AFTER F1-F4 APPROVE)
  - **Blocks**: NOTHING (terminal task — nothing depends on T42)
  - **Blocked By**: T1-T41 ALL COMPLETE **AND** F1-F4 ALL `APPROVE` (the Final Verification Wave runs on the pre-release artifact set; once all 4 reviewers approve, the agent ticks F1-F4 boxes and then executes T42 to ship the tag)

  **References**:
  - `docs/release.md` (T35) — operator runbook
  - `docs/pending-publisher-bootstrap.md` (T39) — must have been executed at least once
  - T38 release workflow
  - T40 release-please

  **Acceptance Criteria**:
  - [ ] `curl -fsSL https://pypi.org/pypi/tiktok-mcp/json | jq -r .info.version` returns "0.1.0"
  - [ ] `uvx tiktok-mcp@0.1.0 --version` exits 0 with "0.1.0" stdout
  - [ ] `git tag --list 'v0.1.0'` returns `v0.1.0`
  - [ ] `gh release view v0.1.0 --json tagName -q .tagName` returns "v0.1.0"
  - [ ] T41 distribution-smoke shows 9/9 green for v0.1.0
  - [ ] CHANGELOG.md has `## [0.1.0]` section with feat/fix entries from all prior tasks

  **QA Scenarios**:

  ```
  Scenario: End-to-end v0.1.0 release ceremony
    Tool: Bash + gh CLI + automated subprocess MCP boot (the Claude Desktop UI smoke is OPERATOR-REQUIRED post-release, NOT part of automated acceptance)
    Preconditions: T1-T41 complete + green
    Steps:
      1. `gh pr list --label "autorelease: pending"` → identify the v0.1.0 PR
      2. `gh pr merge <num> --squash`
      3. `gh run watch --exit-status` (release-please follow-up, then release.yml, then distribution-smoke)
      4. `curl -fsSL https://pypi.org/pypi/tiktok-mcp/0.1.0/json | jq .info.version` → "0.1.0"
      5. In a tmpdir: `uvx tiktok-mcp@0.1.0 --version` → exit 0
      6. In a tmpdir, simulate the Claude Desktop launch via subprocess: `python -c "
import json, subprocess
proc = subprocess.Popen(['uvx', 'tiktok-mcp@0.1.0'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
# Send tools/list JSON-RPC frame
frame = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}) + '\n'
proc.stdin.write(frame.encode())
proc.stdin.flush()
out = proc.stdout.readline()
resp = json.loads(out)
assert 'result' in resp and 'tools' in resp['result']
assert len(resp['result']['tools']) >= 40, f'expected >=40 tools, got {len(resp["result"]["tools"])}'
proc.terminate()
print('OK — production wheel boots stdio MCP, tools/list returns N tools')
"`
      7. Verify `add_account` tool is in the returned list: `python -c "<the above + grep>"`

    > **Operator post-release external step (per AGENTS.md):** actually installing into a real Claude Desktop instance and visually confirming the UI is OPERATOR-ONLY post-release validation. It lives in `.sisyphus/evidence/operator-unblock-checklist.md`, NOT in the agent's automated acceptance criteria. The above subprocess-based QA verifies the equivalent functional behavior automatically.

    Expected Result: subprocess JSON-RPC `tools/list` returns ≥40 tools including `add_account`
    Evidence: .omo/evidence/task-42-end-to-end-release.txt

  Scenario: Idempotent re-run (re-pushing the same tag does NOT republish)
    Tool: Bash + gh CLI
    Preconditions: v0.1.0 already on PyPI
    Steps:
      1. `git tag -d v0.1.0 && git tag v0.1.0` (recreate locally)
      2. `git push --force origin v0.1.0` → assert push succeeds (force is fine for tag)
      3. `gh run watch` → assert release.yml triggers
      4. Assert the OIDC publish step FAILS with "File already exists" (PyPI prevents republish)
      5. Assert the workflow's post-publish smoke step is SKIPPED (because publish failed)
      6. Assert PyPI still serves the original v0.1.0
    Expected Result: PyPI's immutability prevents accidental overwrite; workflow surfaces the conflict clearly
    Evidence: .omo/evidence/task-42-idempotent-rerun.txt
  ```

  **Commit**: NO new commit; this task ships an existing commit (the one that landed via release-please merge). The git tag IS the artifact.

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE before the v0.1.0 git tag is pushed.

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read this plan end-to-end. For each "Must Have": verify implementation exists (read file, run command). For each "Must NOT Have": grep the codebase for forbidden patterns — reject with file:line. Check evidence files exist in `.omo/evidence/`. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  Run `uv run ruff check`, `uv run mypy src/`, `uv run pytest`. Review all changed files for: `typing.cast` bypassing errors, empty excepts, `print()` in src, commented-out blocks, unused imports, AI slop (over-abstraction, generic names, doc-clutter, premature factories).
  Output: `Lint [PASS/FAIL] | Types [PASS/FAIL] | Tests [N pass/N fail] | Files [N clean/N issues] | VERDICT: APPROVE/REJECT`

- [x] F3. **Real Manual QA** — `unspecified-high` (+ `playwright`/`interactive_bash` skills)
  Start from clean state. Execute EVERY QA scenario from EVERY task — capture evidence. Spin up the stdio MCP under Claude Desktop's actual `claude_desktop_config.json` and exercise key flows: add account, list videos, run report, post comment reply (write-gated, demonstrate both block + allow paths), upload video to drafts. Edge cases: write env-var unset, locked keychain, invalid pasted URL, expired state.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT: APPROVE/REJECT`

- [x] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff (`git log --all --diff-filter=AM`). Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Check "Must NOT do" compliance. Detect cross-task contamination (Task N touching Task M's files). Flag unaccounted changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT: APPROVE/REJECT`

---

## Commit Strategy

- One commit per task. Conventional Commits format: `<type>(<scope>): <description>`.
- Types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `ci`, `build`.
- Scopes: `auth`, `display`, `marketing`, `comments`, `posting`, `keychain`, `redact`, `cli`, `ci`, `release`, `docs`, `tests`, `inventory`.
- Pre-commit (per task): the task's specific test command must pass.
- Wave boundaries: tag `wave-N-complete` after each wave's Final QA cleared.
- Final release: `git tag v0.1.0` triggers release workflow.

---

## Success Criteria

### Verification Commands

```bash
# Tests
uv run pytest tests/ --tb=short    # Expected: all green; ≥17 Metis-mandated cases present

# Type + lint
uv run ruff check src/             # Expected: 0 errors
uv run mypy src/                   # Expected: 0 errors (strict mode)

# Local install + smoke
uv run tiktok-mcp --version        # Expected: 0.1.0 (or pre-release)

# uvx smoke (after publish)
uvx tiktok-mcp@0.1.0 --version     # Expected: 0.1.0

# MCP boot
python -c "from tiktok_mcp.server import app; app"  # Expected: no exception, server object exists

# CI matrix
gh workflow view ci.yml --json conclusion          # Expected: success
gh workflow view release.yml --json conclusion     # Expected: success after tag push

# PyPI presence
curl -fsSL https://pypi.org/pypi/tiktok-mcp/json | jq .info.version   # Expected: 0.1.0
```

### Final Checklist
- [ ] All Wave-0 gating spikes returned successful results
- [ ] All Wave 1-5 task acceptance criteria met
- [ ] All 17 Metis-mandated pytest cases present and passing
- [ ] All "Must Have" items shipped
- [ ] All "Must NOT Have" items absent (verified by F4)
- [ ] F1-F4 all APPROVE
- [ ] `pypi.org/project/<name>/` returns 200 with v0.1.0
- [ ] `claude_desktop_config.json` example loads in Claude Desktop without error
