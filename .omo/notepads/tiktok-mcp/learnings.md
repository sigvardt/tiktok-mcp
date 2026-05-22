# Learnings — tiktok-mcp

Condensed cross-cutting insights extracted from the Decisions of Record. Any
sub-agent picking up a task in this plan should skim this file first. It is a
fast-read summary, not a substitute for `decisions.md` (verbatim DoR) or the
plan file itself.

Format: each bullet captures one insight plus the DoR section it derives from.
Append new learnings as work progresses; never overwrite.

## OAuth and account flow

- Manual-paste OAuth flow only. No localhost listener, no hosted relay, no infrastructure. TikTok consoles refuse `localhost` and IP redirect URIs and only accept registered TLDs (per §3).
- OAuth state lives in-memory as `dict[state_token, OAuthInProgress(...)]` with a 10-minute TTL and single-use semantics. An MCP restart loses in-flight states; tell the user to restart the flow (per §11).
- Two-step `remove_account` with a `confirmation_token` (60s TTL). `add_account` returns a suggested alias `<country>-<type>-<short_id>` that the user can override at `complete_account_login`; duplicate aliases are rejected with a suggested next-available suffix (per §14).
- Drafts-default for the Content Posting API. Direct Post requires explicit opt-in via `publish_immediately=True`; otherwise videos and photos land in the user's TikTok draft inbox (per §1).

## Safety gates and env vars

- `TIKTOK_MCP_ALLOW_WRITES` has per-API granularity. Accepted values are unset/empty/0/false (all blocked), 1/true/all (all enabled), or comma-separated subset like `marketing,comments`. See the truth table in §4 for the full matrix and the structured `writes_disabled` error envelope returned on a blocked call.
- `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` is a separate, binary gate orthogonal to `TIKTOK_MCP_ALLOW_WRITES`. It gates `add_account`, `remove_account`, `rename_account`, and app-credential management tools. Account-change tools use `@require_account_changes_enabled` (not `@require_writes_enabled`), an explicit documented exception to §4 justified in §19.
- `TIKTOK_MCP_ALLOW_LIVE_WRITES` is a test-only gate. CI never sets it; write tests run vcrpy-replay only. Local manual override is `=1` (per §4 and §16).

## Token storage and concurrency

- Primary token store is OS keychain via `keyring`. Auto-fallback on `NoKeyringError` is an AES-encrypted JSON file via `cryptography.fernet`, under `platformdirs.user_data_dir("tiktok-mcp")`, with the fernet key stored in keychain when possible. Plain files are never acceptable (per §5).
- Sandbox vs production keychain namespacing is mandatory: `tiktok-mcp::<api>::sandbox::<alias>` vs `tiktok-mcp::<api>::production::<alias>`. Accounts are tagged `sandbox=true` and cross-mode access is rejected (per §5).
- Atomic refresh-token rotation: write the new RT BEFORE discarding the old. Per-account `asyncio.Lock` on the refresh path, with the in-memory token cache write and the keychain write under the same lock (per §5 and §6).
- Concurrency rule of thumb: parallel reads on the same account are fine, parallel writes are not serialized by the MCP (documented as unspecified ordering), parallel calls across accounts run fully in parallel because each account has its own lock (per §6).

## HTTP, errors, and rate limits

- BusinessApi envelope quirk: `HTTP 200 + code != 0` is the standard business-error pattern. A single `BusinessApiResponse` decoder handles Business / Marketing / Comments and raises typed `BusinessApiError(code, message, request_id, context)`. Display API and Content Posting use a separate `DisplayApiResponse` decoder for RESTful errors (per §8). This is the single biggest divergence from REST norms in TikTok's stack.
- httpx exceptions must be wrapped to strip response bodies before bubbling up, to prevent token or PII leakage (per §8).
- Reactive rate limiting only in v0.1. Respect the `Retry-After` header on 429, apply exponential backoff with jitter, max 3 retries, then return a structured `rate_limited` error. A `get_rate_limit_status` MCP tool exposes the posture (per §7).
- Native pagination passthrough per API. No unified abstraction. Cursor for Display, page for Marketing and Comments, polling for Content Posting (per §9).

## Logging, redaction, and PII

- `SecretRedactor` is a `logging.Filter` on the root logger. Seed patterns are `access_token=`, `refresh_token=`, `code=`, `client_secret=`, `Authorization:`. Every token read from keychain is added to the runtime redaction set and replaced with `<REDACTED:token_name>`. A unit test must assert that no token appears in `caplog` or in exception strings (per §12).
- Comment text is never cached or persisted to disk. vcrpy cassettes for comment tests scrub comment-body fields before commit. `--log-level=DEBUG` does not dump comment bodies; explicit opt-in is `TIKTOK_MCP_LOG_COMMENT_BODIES=1` (per §13).
- Logging defaults to stderr at INFO. Env knobs are `TIKTOK_MCP_LOG_LEVEL`, `TIKTOK_MCP_LOG_FILE`, `TIKTOK_MCP_LOG_FORMAT=json`. `SecretRedactor` is always on and cannot be disabled (per §18).

## Testing

- vcrpy default scrubbing does NOT strip tokens. Cassettes must be configured explicitly with `filter_headers=[("Authorization", "REDACTED")]` and a `before_record_response` body sanitizer. A leaked cassette has been a real-world breach pattern (per §16 and Wave 0 spike S3 notes).
- Test stack is `pytest` + `pytest-asyncio` + `vcrpy`. Read tools combine TDD with live CI integration. Write tools are TDD with vcrpy-replay-only in CI; live writes are behind `TIKTOK_MCP_ALLOW_LIVE_WRITES=1` for local manual runs only. Agent-Executed QA Scenarios are mandatory on every task (per §16).
- T4 redaction gotcha (2026-05-22): logging filters see `record.msg` and `record.args` before `%` formatting, so a prefix in the message (`Authorization: Bearer %s`) and an unregistered token in args can otherwise straddle the scan boundary. The redactor must sanitize args and then re-scan the formatted message while avoiding placeholder-looking values like `%s` in the first pass.
- T10 account-onboarding gotcha (2026-05-22): Wave-1 `AppCredentials` currently stores `api_type`, `sandbox`, `client_id`, and `client_secret`, but the manual-paste OAuth flow also needs a registered `redirect_uri` for auth URL construction and host validation. T10 therefore reads `redirect_uri` from the keychain JSON wrapper around app credentials without changing the Wave-1 model.

## PyPI name decision (2026-05-22)

Atlas verified availability before delegating T1:
- `curl https://pypi.org/pypi/tiktok-mcp/json` -> 404 -> AVAILABLE
- `curl https://pypi.org/pypi/tiktok-complete-mcp/json` -> 404 -> AVAILABLE
- `curl https://test.pypi.org/pypi/tiktok-mcp/json` -> 404 -> AVAILABLE

Per DoR §15 decision rule ("`tiktok-mcp` if available, else `tiktok-complete-mcp`"),
the canonical PyPI name is **`tiktok-mcp`**. T1's specific dependency on S2
("need PyPI name decision") is satisfied without waiting on the full S2 runbook.
S2's remaining work (TestPyPI publisher registration, OIDC binding, vSPIKE tag)
is still operator-blocked and remains `- [~]`.

Wave 5 T39 (pending-publisher registration) MUST use `tiktok-mcp` as the project
name when the operator fills the PyPI form.

## T14 Business API client (2026-05-22)

- `BusinessAPIClient` records Business API rate posture through the existing T19 tracker using the account's concrete `ApiType`; Marketing and Business Organic callers should therefore pass the persisted account type rather than a synthetic string.
- The T14 implementation keeps the sanitizer response hook installed and adds its rate-limit hook before it, so HTTP 429 `Retry-After` is captured before sanitized status handling raises.
- The current `AccountTokens.refresh_token` type is non-optional; T14 treats an empty secret value as an absent Business refresh token and raises `AccountBrokenError` with `re_auth_required=True` without calling refresh.
- The operator S3 cassette was not present during T14 verification, so the production-client compatibility test is a no-network replay stub that skips until `spikes/cassettes/s3_business_error.yaml` exists.

## T1+T2 deviation: author email (2026-05-22)

The T1+T2 subagent fabricated `[email protected]` for the pyproject
`authors` email because (per their reasoning) the original prompt value was
"not RFC-valid and blocked hatchling metadata parsing." `signikant.com`
emails ARE RFC-valid; the rejection was probably a stale-tool artifact. Atlas
corrected to `[email protected]` (the user's actual email per session
context) in a follow-up commit.

Lesson for future Wave-1+ delegations: when injecting user-provided values
(emails, names, IDs) into config files, read them out of the active session
context rather than relying on prompt-embedded literals that may have been
mangled in transit. The session context provides the canonical value.

## Packaging and release

- Build backend is `hatchling`; version source is `hatch-vcs` (driven by git tags like `v0.1.0`); release trigger is `on: push: tags: ['v*']`. Publishing uses GitHub Actions OIDC trusted publishing (`pypa/gh-action-pypi-publish@release/v1`); no API tokens anywhere. Python 3.11+, MIT license, entry point `tiktok-mcp = "tiktok_mcp.server:main"`. PyPI name is `tiktok-mcp` if available, else `tiktok-complete-mcp` (per §15).

## T18 Content Posting read tools (2026-05-22)

- Content Posting read calls use Login Kit bearer auth (`Authorization: Bearer <token>`) against `https://open.tiktokapis.com`, with Display-style response envelopes decoded by `decode_display_response`.
- `posting_get_post_status` validates status as the fixed enum `PROCESSING_DOWNLOAD | PROCESSING_UPLOAD | PROCESSING_PUBLISH | PUBLISH_COMPLETE | FAILED | EXPIRED`; keep this strict because T26 upload progress depends on reliable polling states.
- `posting_get_creator_info` is intentionally uncached because creator privacy options and upload limits can change in the TikTok app immediately before upload init.
- TikTok has not exposed a public v2 drafts-list endpoint as of 2026-05-22. T18 keeps `posting_list_drafts` registered for discoverability but returns `endpoint_not_available` rather than guessing an endpoint.

## T12 Display API client findings (2026-05-22)

- The shipped T5 keychain surface is `get_backend()` + `account_key()` + `deserialize_account_record()` + `atomic_account_update()`, not a `Keychain.get_account_tokens` class API. T12 therefore preserves the intended T5 guarantees by using those helpers directly and never touching keyring primitives.
- Display API client refreshes use a per-account in-memory `asyncio.Lock` keyed by `(api_type, sandbox, alias)`. The lock wraps the reload-from-keychain, token endpoint call, atomic keychain write, and in-memory token replacement so concurrent expired-token requests dedupe to one refresh.
- Display API request auth is `Authorization: Bearer <access_token>` only. `Access-Token` remains Business API-specific and must not be used for Display reads.
- Refresh-token rotation is represented by one `atomic_account_update(...)` call that writes the full `Account` + `AccountTokens` record, including both new access token and new refresh token when TikTok rotates it. If the keychain write fails, keep the old in-memory tokens and raise instead of silently discarding the old refresh token.
- T19 rate-limit tracker currently exposes `record_request(api_type, alias)` and `record_429(api_type, alias, retry_after_seconds)`, despite the plan text using shorter keyword names. Use the shipped `ApiType.DISPLAY` API surface.

## T17 Business Organic comment reads (2026-05-22)

- T17 implemented only `comments_list` and `comments_list_replies` because `docs/api-surface-inventory.md` keeps `comments_get` as an open issue without a v0.1 endpoint row. Delayed endpoint research verified public-doc paths with `/business/comment/...`, so the tools and cassettes use `/open_api/v1.3/business/comment/list/` and `/open_api/v1.3/business/comment/reply/list/`.
- Comment read tests author replay cassettes in-place with `[SCRUBBED]` comment bodies and a `before_record_response` scrubber that recursively replaces `text` and `comment_text`. Keep `grep -nE "text:.{50,}" tests/cassettes/comments_*.yaml` as the quick no-leak check.
- Runtime comment logging records metadata and IDs at INFO only. Raw comment bodies appear only when the logger is DEBUG-enabled and `TIKTOK_MCP_LOG_COMMENT_BODIES=1`; all other modes log a redacted DEBUG placeholder and no text body.

## T15 Marketing API read tools (2026-05-22)

- Marketing read tools load the persisted `ApiType.MARKETING` account by alias, then same-sandbox app credentials, and pass the account, app credentials, and tokens into T14 `BusinessAPIClient`; tools should not construct raw Business API `httpx` calls.
- Campaign/adgroup/ad list tools return the decoded upstream data dict unchanged so native Marketing pagination keys (`page`, `page_size`, `total_number`, `total_page`) pass through exactly. Single-entity get tools reuse the list endpoints with the relevant ID array inside `filtering` plus `page=1` and `page_size=1`.
- `marketing_list_bc_advertisers` uses `/open_api/v1.3/bc/asset/get/` with `asset_type=ADVERTISER`; integration coverage asserts distinct `Access-Token` headers per account and configures cassette scrubbing for `Access-Token`.

## T13 Display read tools (2026-05-22)

- Display read MCP tools should remain thin wrappers around `DisplayAPIClient.request`; tests can inject `httpx.MockTransport` by monkeypatching `DisplayAPIClient._build_http_client`, which preserves the production auth/decoder/refresh path while avoiding real network calls.
- User-info field requests must be scope-filtered before calling TikTok and scope-gated again before returning data. `user.info.basic` covers `open_id`, `avatar_url`, and `display_name`; profile/stat fields stay optional so narrow-scope tokens validate without leaking unavailable fields.
- `display_revoke_token` is a Display write-gated utility, not an account deletion path: call the revoke endpoint, then persist `AccountStatus.REVOKED` through `atomic_account_update(...)` with the existing token record intact.

## T16 Marketing reports (2026-05-22)

- Marketing report validation now uses `REPORT_MAX_DATE_RANGE_DAYS`: BASIC=365 days, AUDIENCE=30 days, PLAYABLE_AD=30 days, with a 30-day conservative default. Validation happens through Pydantic before `BusinessAPIClient` construction, so enum/date failures make zero HTTP requests.
- T16 report rows preserve upstream values but enforce explicit per-row `currency_code` and `timezone`; sync responses and streamed CSV downloads raise if those fields cannot be surfaced. CSV downloads stream through httpx and parse in memory only, with no disk persistence.

## [2026-05-22T13:51:15Z] Task: T22

- Marketing ad write tools keep `CreateAdRequest`/`UpdateAdRequest` inline and return a `validation_error` dict before constructing `BusinessAPIClient` when Pydantic rejects inputs; the spark-ads rule (`creative_authorized=True` requires `spark_ads_post_id`) is therefore zero-HTTP.
- `update_ad_status` and `delete_ad` expose one `ad_id` per tool call and translate it to the Marketing API's `ad_ids` array payload, preserving the no-bulk-create constraint while matching the write endpoints.
- Ad write replay cassettes live under `tests/cassettes/marketing_ads/` and scrub `Access-Token`; tests use `httpx.MockTransport` to replay cassette responses without any real HTTP.

## 2026-05-22 Task: T20

- Campaign CRUD write models stay inline in `marketing_writes_campaigns.py` so Wave 3 write tasks do not collide with the shared Marketing read models.
- `require_writes_enabled` still owns the gate; T20 adds a tiny outer wrapper only to enrich blocked campaign writes with endpoint/action `would_have_done` metadata and the required INFO log without changing the shared decorator contract.
- Unit tests use `BusinessAPIClient` plus `httpx.MockTransport` for request-body/header assertions; replay integration tests use vcrpy `record_mode="none"` cassettes directly, which intercept httpx before any real network call.

## [2026-05-22T15:50:00Z] Task: T21

- AdGroup write tools keep request models inline in `marketing_writes_adgroups.py`; do not extend shared Marketing models for this Wave-3 surface.
- Nordic geo validation is enforced by the nested Pydantic targeting block before `BusinessAPIClient` construction, so invalid `targeting.locations` values produce a `validation_error` envelope and fire zero HTTP requests.
- Replay tests use vcrpy-format cassettes under `tests/cassettes/marketing_adgroups/` and explicitly scrub `Access-Token`, while runtime write calls still go only through `BusinessAPIClient.request`.

## [2026-05-22 15:52] Task: T25

- Comment write tools use only the verified Business Organic endpoint family `/open_api/v1.3/business/comment/...` for reply create, pin/unpin, hide/unhide, and own-reply delete.
- `@require_writes_enabled("comments")` is the literal per-API gate for all six moderation writes; `TIKTOK_MCP_ALLOW_WRITES=marketing` must still block them while `comments` and `all` allow them.
- Reply text validation happens before client construction or HTTP: `len()` must be at most 150, surrogate code points are rejected, and the accepted body is NFC-normalized.
- Reply/comment text must stay out of INFO/WARN logs and committed cassettes. Opt-in DEBUG logging is still gated by `TIKTOK_MCP_LOG_COMMENT_BODIES=1`, and write cassettes scrub both response and request `text`/`comment_text` fields.
- v0.1 keeps comment/account ownership validation as a pre-HTTP hook because T17 shipped list/reply-list only and no single-comment lookup endpoint; replace the hook with a live lookup if TikTok exposes one later.
## [2026-05-22T13:54:08Z] Task: T23

- Custom Audience uploads stream plaintext CSV rows through `HashedAudienceCSVStream`, lower/trim emails, strip phone separators, SHA-256 hash in memory, and hand httpx a reusable file-like object so an auth-refresh retry can seek back to the start without writing hashes to disk.
- Audience upload path validation rejects network URLs, raw `..` segments, non-files, files outside both `Path.home()` and `Path.cwd()`, and files over 100MB before constructing the Business API client, so invalid-path tests make zero outbound HTTP.
- Audience upload INFO logging should stay to structured metadata only: `filename_hash`, `row_count_estimate`, and `file_size_bytes`; tests assert fixture plaintext emails never appear in caplog or multipart bodies.

