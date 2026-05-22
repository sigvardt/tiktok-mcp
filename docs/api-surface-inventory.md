# API Surface Inventory (v0.1)

## How to read this

This document is the canonical enumeration of every MCP tool shipping in `tiktok-mcp` v0.1, organised by the four TikTok API surfaces in scope (Display, Marketing, Business Organic, Content Posting) plus a Setup section for OAuth and observability tooling. Wave 2 and Wave 3 task specifications in `.omo/plans/tiktok-mcp.md` read FROM this inventory; per-tool work cannot start until every row here is fixed.

Column meanings:

1. **MCP tool name**: the function exported from a module under `src/tiktok_mcp/tools/`. Snake case, prefix conventions enforced by T36 lint (reads start with `get_`/`list_`/`describe_`/`search_`/`verify_`; writes start with `create_`/`update_`/`delete_`/`pause_`/`resume_`/`upload_`/`post_`/`pin_`/`unpin_`/`hide_`/`unhide_`/`revoke_`/`refresh_`/`publish_`/`move_`/`finalize_`/`cancel_`; account-change tools have their stipulated names per DoR §14).
2. **TikTok endpoint**: the canonical request path on either `https://open.tiktokapis.com` (Display + Content Posting) or `https://business-api.tiktok.com` (Marketing + Business Organic). Unknown or unconfirmed paths are pushed into the `Open issues` section, not embedded as ambiguous cells.
3. **HTTP method**: GET, POST, or PUT, as TikTok documents the endpoint.
4. **Required scope**: OAuth scope name from the TikTok developer or business portal. `—` for tools that do not invoke TikTok (Setup, rate limit observability). `(see Open issues)` if the scope mapping needs verification.
5. **Annotation**: `readOnlyHint` or `destructiveHint`, exactly as required by DoR §4 and the compile-time check from T8.
6. **Writes namespace**: the value `TIKTOK_MCP_ALLOW_WRITES` must contain (or be `all`) for this tool to run. One of `display`, `marketing`, `comments`, `posting`, `account-changes`, or `—`. The `account-changes` value is the orthogonal `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` gate from DoR §19, distinct from the per-API write gate. `—` is for read-only tools.
7. **Wave**: `2` for read-side delivery plus Setup; `3` for write-side delivery.
8. **Task**: forward reference to the plan task that ships the tool, matching the wave allocation under "Parallel Execution Waves" in `.omo/plans/tiktok-mcp.md`.

Tool count expectation: 60 to 80 total across all surfaces. The current grand total is reported at the bottom under `Tool count by surface`.

Excluded surfaces (Catalog Manager, Audience Segments / Lookalike, Reservation buying, Pixel / Events API, Research API, comment search, interactive slideshow, scraping, etc.) are listed with a one-line reason in `Excluded (deferred to v0.2)`. Any endpoint whose canonical 2026 path could not be confirmed against the plan task specs is listed in `Open issues` with the expected verification scope. Open issues block Wave 2 commencement for the affected tools only; they need a short librarian-style verification spike before per-tool implementation kicks off.

MCP Resources (`tiktok-mcp://accounts/` and `tiktok-mcp://app-credentials/`) ship in T29 and are counted in the Setup section as a note, not as MCP tools.

## Display API

Source: T13 (reads, Wave 2). Two token-utility tools are destructive (`display_refresh_token`, `display_revoke_token`) and gated by `@require_writes_enabled("display")` per DoR §4, since they mutate keychain or TikTok-side authorisation state.

| MCP tool name | TikTok endpoint | HTTP method | Required scope | Annotation | Writes namespace | Wave | Task |
|---|---|---|---|---|---|---|---|
| display_get_user_info | /v2/user/info/ | POST | user.info.basic | readOnlyHint | — | 2 | T13 |
| display_list_videos | /v2/video/list/ | POST | video.list | readOnlyHint | — | 2 | T13 |
| display_query_videos | /v2/video/query/ | POST | video.list | readOnlyHint | — | 2 | T13 |
| display_get_video_metrics | /v2/video/query/ | POST | video.list | readOnlyHint | — | 2 | T13 |
| display_refresh_token | /v2/oauth/token/ | POST | — | destructiveHint | display | 2 | T13 |
| display_revoke_token | /v2/oauth/revoke/ | POST | — | destructiveHint | display | 2 | T13 |

## Marketing API

Sources: T15 (advertiser, BC, campaign/adgroup/ad reads), T16 (reports), T20 (campaign writes), T21 (adgroup writes), T22 (ad writes), T23 (custom audience writes), T24 (creative asset writes). All Marketing writes are decorated `@require_writes_enabled("marketing")`. All Marketing requests use the `Access-Token: <token>` header convention from the BusinessAPIClient (T14), not `Authorization: Bearer`.

| MCP tool name | TikTok endpoint | HTTP method | Required scope | Annotation | Writes namespace | Wave | Task |
|---|---|---|---|---|---|---|---|
| marketing_list_advertisers | /open_api/v1.3/oauth2/advertiser/get/ | GET | user.info.basic | readOnlyHint | — | 2 | T15 |
| marketing_get_advertiser_info | /open_api/v1.3/advertiser/info/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_list_business_centers | /open_api/v1.3/bc/get/ | GET | business.bc.read | readOnlyHint | — | 2 | T15 |
| marketing_list_bc_advertisers | /open_api/v1.3/bc/asset/get/ | GET | business.bc.read | readOnlyHint | — | 2 | T15 |
| marketing_list_campaigns | /open_api/v1.3/campaign/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_list_adgroups | /open_api/v1.3/adgroup/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_list_ads | /open_api/v1.3/ad/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_get_campaign | /open_api/v1.3/campaign/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_get_adgroup | /open_api/v1.3/adgroup/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_get_ad | /open_api/v1.3/ad/get/ | GET | business.advertiser.read | readOnlyHint | — | 2 | T15 |
| marketing_run_sync_report | /open_api/v1.3/report/integrated/get/ | GET | business.report.read | readOnlyHint | — | 2 | T16 |
| marketing_run_async_report | /open_api/v1.3/report/task/create/ | POST | business.report.read | readOnlyHint | — | 2 | T16 |
| marketing_poll_async_report | /open_api/v1.3/report/task/check/ | GET | business.report.read | readOnlyHint | — | 2 | T16 |
| marketing_download_async_report | (signed file_url from poll) | GET | business.report.read | readOnlyHint | — | 2 | T16 |
| create_campaign | /open_api/v1.3/campaign/create/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T20 |
| update_campaign | /open_api/v1.3/campaign/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T20 |
| update_campaign_status | /open_api/v1.3/campaign/status/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T20 |
| delete_campaign | /open_api/v1.3/campaign/delete/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T20 |
| create_adgroup | /open_api/v1.3/adgroup/create/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T21 |
| update_adgroup | /open_api/v1.3/adgroup/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T21 |
| update_adgroup_status | /open_api/v1.3/adgroup/status/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T21 |
| delete_adgroup | /open_api/v1.3/adgroup/delete/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T21 |
| create_ad | /open_api/v1.3/ad/create/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T22 |
| update_ad | /open_api/v1.3/ad/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T22 |
| update_ad_status | /open_api/v1.3/ad/status/update/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T22 |
| delete_ad | /open_api/v1.3/ad/delete/ | POST | business.advertiser.write | destructiveHint | marketing | 3 | T22 |
| create_custom_audience | /open_api/v1.3/dmp/custom_audience/create/ | POST | business.audience.write | destructiveHint | marketing | 3 | T23 |
| update_custom_audience_name | /open_api/v1.3/dmp/custom_audience/update/ | POST | business.audience.write | destructiveHint | marketing | 3 | T23 |
| delete_custom_audience | /open_api/v1.3/dmp/custom_audience/delete/ | POST | business.audience.write | destructiveHint | marketing | 3 | T23 |
| upload_video_asset | /open_api/v1.3/file/video/ad/upload/ | POST | business.creative.write | destructiveHint | marketing | 3 | T24 |
| upload_image_asset | /open_api/v1.3/file/image/ad/upload/ | POST | business.creative.write | destructiveHint | marketing | 3 | T24 |
| delete_video_asset | /open_api/v1.3/file/video/ad/delete/ | POST | business.creative.write | destructiveHint | marketing | 3 | T24 |
| delete_image_asset | /open_api/v1.3/file/image/ad/delete/ | POST | business.creative.write | destructiveHint | marketing | 3 | T24 |

## Business Organic

Source: T17 (comment reads, Wave 2), T25 (comment writes, Wave 3). All writes decorated `@require_writes_enabled("comments")`. Comment text never logs at INFO and never persists to disk per DoR §13; vcrpy cassettes scrub comment bodies before commit.

| MCP tool name | TikTok endpoint | HTTP method | Required scope | Annotation | Writes namespace | Wave | Task |
|---|---|---|---|---|---|---|---|
| comments_list | /open_api/v1.3/comment/list/ | GET | business.comment.management | readOnlyHint | — | 2 | T17 |
| comments_list_replies | /open_api/v1.3/comment/reply/list/ | GET | business.comment.management | readOnlyHint | — | 2 | T17 |
| post_comment_reply | /open_api/v1.3/comment/reply/create/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |
| pin_comment | /open_api/v1.3/comment/pin/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |
| unpin_comment | /open_api/v1.3/comment/pin/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |
| hide_comment | /open_api/v1.3/comment/hide/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |
| unhide_comment | /open_api/v1.3/comment/hide/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |
| delete_own_reply | /open_api/v1.3/comment/reply/delete/ | POST | business.comment.management | destructiveHint | comments | 3 | T25 |

## Content Posting

Source: T18 (read tools, Wave 2), T26 (chunked FILE_UPLOAD writes, Wave 3), T27 (PULL_FROM_URL + photo writes + opt-in direct post, Wave 3), T28 (draft management writes, Wave 3). All writes decorated `@require_writes_enabled("posting")`. Drafts-default for every upload tool per DoR §1; direct post requires explicit `publish_immediately=True` plus a complete `post_info` block.

| MCP tool name | TikTok endpoint | HTTP method | Required scope | Annotation | Writes namespace | Wave | Task |
|---|---|---|---|---|---|---|---|
| posting_get_post_status | /v2/post/publish/status/fetch/ | POST | video.upload | readOnlyHint | — | 2 | T18 |
| posting_list_drafts | (no public v2 drafts-list endpoint) | — | video.upload | readOnlyHint | — | 2 | T18 |
| posting_get_creator_info | /v2/post/publish/creator_info/query/ | POST | video.upload | readOnlyHint | — | 2 | T18 |
| list_pending_drafts | /v2/post/publish/inbox/video/list/ | POST | video.upload | readOnlyHint | — | 2 | T28 |
| init_video_upload | /v2/post/publish/inbox/video/init/ | POST | video.upload | destructiveHint | posting | 3 | T26 |
| upload_video_chunk | (TikTok-issued upload_url) | PUT | video.upload | destructiveHint | posting | 3 | T26 |
| finalize_video_upload | /v2/post/publish/status/fetch/ | POST | video.upload | destructiveHint | posting | 3 | T26 |
| upload_video_from_url | /v2/post/publish/inbox/video/init/ | POST | video.upload | destructiveHint | posting | 3 | T27 |
| upload_photo_from_urls | /v2/post/publish/inbox/photo/init/ | POST | video.upload | destructiveHint | posting | 3 | T27 |
| get_publish_status | /v2/post/publish/status/fetch/ | POST | video.upload | readOnlyHint | — | 3 | T27 |
| cancel_publish | /v2/post/publish/cancel/ | POST | video.upload | destructiveHint | posting | 3 | T27 |
| move_draft_to_publish | /v2/post/publish/video/init/ | POST | video.publish | destructiveHint | posting | 3 | T28 |
| delete_draft | /v2/post/publish/cancel/ | POST | video.upload | destructiveHint | posting | 3 | T28 |

## Setup

Source: T10 (account management tools, Wave 2), T11 (app credential tools, Wave 2), T19 (rate limit observability, Wave 2). Account-management writes use `@require_account_changes_enabled` (the orthogonal `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` gate per DoR §19), captured here under the Writes namespace `account-changes`. No TikTok endpoint is invoked directly by Setup tools; their work is local keychain management plus, in the case of OAuth completion and `verify_app_credentials`, an ephemeral token-exchange or token-introspection call whose path is determined by the chosen `api_type`.

| MCP tool name | TikTok endpoint | HTTP method | Required scope | Annotation | Writes namespace | Wave | Task |
|---|---|---|---|---|---|---|---|
| add_account | (none, local + OAuth URL builder) | POST | — | destructiveHint | account-changes | 2 | T10 |
| complete_account_login | (per api_type token endpoint) | POST | — | destructiveHint | account-changes | 2 | T10 |
| list_accounts | (none, local keychain read) | GET | — | readOnlyHint | — | 2 | T10 |
| rename_account | (none, local keychain rewrite) | POST | — | destructiveHint | account-changes | 2 | T10 |
| remove_account | (none, local keychain delete) | POST | — | destructiveHint | account-changes | 2 | T10 |
| set_app_credentials | (none, local keychain write) | POST | — | destructiveHint | account-changes | 2 | T11 |
| list_app_credentials | (none, local keychain read) | GET | — | readOnlyHint | — | 2 | T11 |
| verify_app_credentials | (per api_type, ephemeral) | POST | — | readOnlyHint | — | 2 | T11 |
| get_rate_limit_status | (none, in-memory tracker) | GET | — | readOnlyHint | — | 2 | T19 |

Note on MCP Resources (T29, Wave 4): `tiktok-mcp://accounts/` and `tiktok-mcp://app-credentials/` ship as read-only MCP Resources, not as MCP tools, and are therefore not counted in any row above. They reuse `list_accounts` and `list_app_credentials` logic respectively. Both return fingerprint-only payloads, never raw secrets.

## Excluded (deferred to v0.2)

The following surfaces are explicitly out of scope for v0.1 per DoR §2. They are listed here so future work has a single place to look for "what did we cut and why".

1. **Catalog Manager / DPA (Marketing API)**: Dynamic product catalogues and dynamic-product-ad creatives need a separate product-feed model and catalogue lifecycle tools. Out of scope to keep v0.1 focused on auction-buying primitives. Deferred to v0.2.
2. **Audience Segments / Lookalike (Marketing API)**: Lookalike audience modelling and segment management have their own object model and seed-audience plumbing. v0.1 ships only file-based custom audiences via `create_custom_audience`. Deferred to v0.2.
3. **Reservation buying (Marketing API)**: v0.1 is auction-only. Reservation campaigns have a distinct planning workflow and pricing model. Deferred to v0.2.
4. **Pixel / Events API (Marketing API)**: Web and app event ingestion needs server-side event hashing, deduplication, and event-quality scoring. Deferred to v0.2.
5. **Comment search (Business Organic)**: TikTok's comment search endpoint requires different query semantics and rate limits than the per-video comment list path. v0.1 ships list and reply only. Deferred to v0.2.
6. **Interactive slideshow (Content Posting)**: Multi-photo single-post upload is in scope via `upload_photo_from_urls`. The interactive slideshow format with auto-advance, music sync, and per-frame transitions is a different post type. Deferred to v0.2.
7. **Research API (academic)**: Different OAuth flow, different rate limit posture, different audience. Deferred to v0.2.
8. **Scraping / no-auth endpoints**: Out of scope across all surfaces. v0.1 supports OAuth-authenticated paths only.
9. **Non-Python clients, web UI, long-term analytics storage, telemetry**: Out of scope across the project per DoR §2.

## Open issues

These rows ship in v0.1 but have an endpoint or scope value that needs a short librarian-style verification spike before the corresponding Wave 2 task begins. They do not block other tools; they block only their own task. Mark each one resolved by editing this section after the verification spike.

1. **`marketing_list_bc_advertisers` endpoint path**: row uses `/open_api/v1.3/bc/asset/get/` based on the path cited in T15. Verify the exact 2026 path against the Business Center docs at `https://business-api.tiktok.com/portal/docs?id=...`. If the path has rotated to `/bc/advertiser/get/` or similar, update the row before T15 starts.
2. **Marketing API scope names**: the rows above use `business.advertiser.read`, `business.advertiser.write`, `business.bc.read`, `business.report.read`, `business.audience.write`, and `business.creative.write` as conventional scope labels. The exact scope strings used by the Business API portal in 2026 must be confirmed and aligned with what `set_app_credentials` validates against. Verify per surface during T11 and T15 prep.
3. **Business Organic comment list endpoint paths**: rows `comments_list` and `comments_list_replies` use `/open_api/v1.3/comment/list/` and `/open_api/v1.3/comment/reply/list/`. Plan task T17 explicitly flags these paths as needing librarian re-verification (research bg_066e9675 was partial). Verify against `https://business-api.tiktok.com/portal/docs?id=1747977406714881` before T17 starts. The write-side paths in T25 are documented and confirmed.
4. **`comments_get` single-comment fetch**: T17 lists this tool as "if endpoint exists; otherwise omit and add to v0.2". No row included above. Confirm endpoint existence during T17 verification. If present, add a row with `readOnlyHint`, scope `business.comment.management`, Wave 2, Task T17. If absent, log a deferral to v0.2 in this section.
5. **`cancel_publish` and `delete_draft` endpoint paths**: rows use `/v2/post/publish/cancel/`. Confirm against `https://developers.tiktok.com/doc/content-posting-api-reference-direct-post` whether the cancel endpoint is shared between in-flight uploads and inbox draft removal, or whether `delete_draft` needs a distinct path.
6. **`list_pending_drafts` endpoint path**: row uses `/v2/post/publish/inbox/video/list/`. T18 marks the draft-list endpoint as "Endpoint TBD". Confirm before T28 starts; if absent, drop the row and surface the gap as a v0.2 deferral here.
7. **`move_draft_to_publish` endpoint path**: row uses `/v2/post/publish/video/init/` (the direct-post endpoint) because T28 references the same publish init endpoint for converting drafts. Confirm whether a dedicated draft-to-publish endpoint exists, or whether the flow truly reuses the direct-post init with a draft `publish_id` parameter.
8. **`posting_get_post_status` vs `get_publish_status` deduplication**: T18 ships `posting_get_post_status` and T27 ships `get_publish_status`. Both call `/v2/post/publish/status/fetch/`. Likely a naming overlap; reconcile before Wave 3 (decide which name survives and update the inventory row). Current rows include both to honour the "use the exact name from the plan task spec" rule.
9. **`posting_list_drafts` endpoint absence**: T18 keeps a registered read-only MCP tool for discoverability, but it returns `{"endpoint_not_available": true, "reason": "TikTok has not exposed a drafts-list endpoint in v2 as of 2026-05-22"}` instead of calling a guessed endpoint. Public Content Posting v2 docs expose inbox upload init and publish status polling, but not a drafts-list read path. Revisit in v0.2 or when TikTok publishes a canonical drafts-list endpoint.

## Tool count by surface

| Surface | Tool count |
|---|---|
| Display API | 6 |
| Marketing API | 33 |
| Business Organic | 8 |
| Content Posting | 13 |
| Setup | 9 |
| **Grand total** | **69** |

Breakdown by annotation: 29 `readOnlyHint`, 40 `destructiveHint`. Breakdown by wave: 33 in Wave 2 (reads plus Setup plus Display token utilities plus `get_publish_status`), 36 in Wave 3 (write-side delivery). The 60 to 80 target band is met. MCP Resources from T29 are counted separately and not included in this rollup.
