# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.2.2 (2026-05-28)

### Bug Fixes

- Marketing token exchange accepts TikTok For Business responses that omit
  `expires_in`; marketing access tokens use TikTok's documented 24-hour
  lifetime while refresh-token TTL still honors `refresh_token_expire_in`.

## 0.2.1 (2026-05-28)

### Bug Fixes

- Route Marketing and Business Organic OAuth authorization, token exchange, and credential probes through `business-api.tiktok.com` even for sandbox accounts.
- Require an explicit `redirect_uri` on stored app credentials for every API surface, including Business API surfaces, so onboarding cannot silently use an unregistered localhost redirect.

## 0.2.0 (2026-05-27)

### Features

- Add `poll_loopback_login` so loopback OAuth setup returns immediately and MCP clients can poll for completion without hitting request timeouts.

### Bug Fixes

- Require explicit redirect URIs for `display` and `content_posting`, while preserving the localhost fallback for Business API surfaces (`business_organic` and `marketing`).
- Document the loopback polling flow and updated redirect behavior.

## 0.1.0 (2026-05-26)


### Features

* **accounts:** add multi-account OAuth setup tools with manual-paste flow and two-step remove ([2b8e64e](https://github.com/sigvardt/tiktok-mcp/commit/2b8e64e1a16c24b0342010bf68af7a97a2afce56))
* **api/business:** add BusinessAPIClient with Access-Token header + envelope decoding ([d2a1341](https://github.com/sigvardt/tiktok-mcp/commit/d2a1341a2849b68e49bf9472c982d9265fe56cbf))
* **api/display:** add DisplayAPIClient with token refresh, rate limit retry, atomic RT rotation ([3135ca7](https://github.com/sigvardt/tiktok-mcp/commit/3135ca763c5158d69456c23c1b4786ed3d2ec1a5))
* **app_credentials:** add set/list/verify tools with fingerprint-only returns ([d2e76f7](https://github.com/sigvardt/tiktok-mcp/commit/d2e76f73f3446730c951dbb6755518008fc348a9))
* **auth:** add keyring + encrypted-file keychain backend with sandbox namespacing ([bfc228d](https://github.com/sigvardt/tiktok-mcp/commit/bfc228d01f7a6072b34d57b3c937a3687891bd93))
* **auth:** add loopback OAuth tool ([ea3a97f](https://github.com/sigvardt/tiktok-mcp/commit/ea3a97f0b8463d01539258106edecff51630db01))
* **auth:** add OAuth state manager with 10-min TTL and single-use replay protection ([bbe0fcd](https://github.com/sigvardt/tiktok-mcp/commit/bbe0fcd1d4af8f95b3aaf2b88a92f16fee412db3))
* **auth:** add SecretRedactor logging filter and httpx exception sanitizer ([37ba212](https://github.com/sigvardt/tiktok-mcp/commit/37ba212ccf7ce18aafd03126683df00c364681b8))
* **auth:** loopback callback capture for localhost redirect_uri (RFC 8252) ([6b24c1f](https://github.com/sigvardt/tiktok-mcp/commit/6b24c1f83949523d5589ead502fb5eaeae60c132))
* **comments:** comment moderation write tools (per-API gated, PII-safe) ([3677d90](https://github.com/sigvardt/tiktok-mcp/commit/3677d9001940dcf06bd514b64d1474678bd3f988))
* **decorators:** add require_writes_enabled with per-API granularity and runtime enforcement ([2746475](https://github.com/sigvardt/tiktok-mcp/commit/27464759eb669ac5420c9c6b9525ed3c90cd679e))
* **display:** auto-enrich list_videos engagement metrics via query pipeline ([727be67](https://github.com/sigvardt/tiktok-mcp/commit/727be675a883f8f99698e2c9bd37ba0a7d85b4b8))
* **envelopes:** add BusinessApiResponse and DisplayApiResponse decoders with typed errors ([264a76a](https://github.com/sigvardt/tiktok-mcp/commit/264a76a9af877bb79d914ba9a1d7af1b6a5f3dab))
* **marketing:** ad CRUD write tools ([413117f](https://github.com/sigvardt/tiktok-mcp/commit/413117f16bc695fdab84bd9a90b9c2b7cdf5d1e0))
* **marketing:** adgroup CRUD write tools ([8be577f](https://github.com/sigvardt/tiktok-mcp/commit/8be577fb36375c2cb02ab4d7da081a10f9333e81))
* **marketing:** campaign CRUD write tools (env-gated, destructive) ([e4e6009](https://github.com/sigvardt/tiktok-mcp/commit/e4e6009521f78185a364bb2148c1e8d94d136675))
* **marketing:** creative library upload tools ([6649ffd](https://github.com/sigvardt/tiktok-mcp/commit/6649ffdc8add58b87cde92a50667c6c5cceca0c6))
* **marketing:** custom audience upload tools (PII-hashed, env-gated) ([4c78777](https://github.com/sigvardt/tiktok-mcp/commit/4c7877732434371efd8388aafaf24c3def3ce423))
* **observability:** add rate-limit tracker module and get_rate_limit_status tool ([4fe354a](https://github.com/sigvardt/tiktok-mcp/commit/4fe354ab22a4629c8bdaefda2f8368f196ecbe32))
* **posting:** chunked video upload tools (drafts default, refresh-aware) ([1406ce7](https://github.com/sigvardt/tiktok-mcp/commit/1406ce75e52445ccefea80b5d1928eb6166a6529))
* **posting:** draft management writes + list_drafts tool ([36a4a83](https://github.com/sigvardt/tiktok-mcp/commit/36a4a83775b6aa99c46a7cab213af02a60a53a57))
* **posting:** pull-from-url + photo upload tools (drafts default) ([5986f81](https://github.com/sigvardt/tiktok-mcp/commit/5986f81a25407ef6069102f7f696f8b0ce0fbfe3))
* **prompts:** weekly_marketing_report + comment_queue_triage + weekly_engagement_summary ([c65c2b5](https://github.com/sigvardt/tiktok-mcp/commit/c65c2b54dc74719478ede23754c0df2c59e47d35))
* **resources:** accounts + app-credentials MCP resources ([e1a3eb4](https://github.com/sigvardt/tiktok-mcp/commit/e1a3eb4968ddd1ce4d3bdc5b4434274e575f8f49))
* **safety:** hard-wired live-account safety kill switch via TIKTOK_MCP_LIVE_ACCOUNT_SAFETY ([227e5e3](https://github.com/sigvardt/tiktok-mcp/commit/227e5e3e5dec7d0ed2efeae10481215844377380))
* **safety:** lock all live surfaces by default (display + marketing + posting + comments) ([020139a](https://github.com/sigvardt/tiktok-mcp/commit/020139a44dd37881b405d77c429df2bc24bea373))
* **server:** stdio entry point + end-to-end boot test ([b1f8526](https://github.com/sigvardt/tiktok-mcp/commit/b1f852674c0f7d7fd8bf07ffd7b843aa35a82226))
* **tools/comments:** add comment read tools with PII scrubbing in cassettes ([5725139](https://github.com/sigvardt/tiktok-mcp/commit/5725139121d9b1c4889f113c9c7ce06ff2f2fb5c))
* **tools/display:** add user_info + video_list/query/metrics + token utilities ([361bea2](https://github.com/sigvardt/tiktok-mcp/commit/361bea28cbff3ed8bf325fc70d563989934983f1))
* **tools/marketing:** add read tools (advertiser, BC, campaign/adgroup/ad list+get) ([27a7290](https://github.com/sigvardt/tiktok-mcp/commit/27a7290e58cd6e7dd7aae5f95c57849fc3ef3fe4))
* **tools/marketing:** add report tools (sync + async create/poll/download) ([7c7299a](https://github.com/sigvardt/tiktok-mcp/commit/7c7299a4509a2d3b09d6e8fdd43489b3c045ad29))
* **tools/posting:** add post status + drafts + creator_info read tools ([3dfdd88](https://github.com/sigvardt/tiktok-mcp/commit/3dfdd88eca92d91aa10683fdbbf5270959cdfb3d))
* **types:** add pydantic v2 models for accounts, OAuth, app credentials, errors ([f169af8](https://github.com/sigvardt/tiktok-mcp/commit/f169af847f58704804e2ee1c15e5231b1621f588))


### Bug Fixes

* **accounts:** add sandbox parameter to account-management tools to enable sandbox OAuth onboarding ([fe54603](https://github.com/sigvardt/tiktok-mcp/commit/fe54603ca62821d1995933baa80e30588042eb5a))
* **api/business:** route sandbox accounts to sandbox-ads.tiktok.com base URL ([bb56304](https://github.com/sigvardt/tiktok-mcp/commit/bb56304966df2052261d9467a6516dc38ae452af))
* **auth:** correct TikTok PKCE challenge ([d4d9737](https://github.com/sigvardt/tiktok-mcp/commit/d4d97379c699235f56acb6db57478edd083f3f62))
* **auth:** make AccountTokens.refresh_token Optional to support pre-minted Business sandbox tokens ([c35d7f3](https://github.com/sigvardt/tiktok-mcp/commit/c35d7f3ed17a062ccb8f811c4e79000c950d5aa3))
* **auth:** surface TikTok OAuth error envelopes + correct PKCE encoding ([2af5451](https://github.com/sigvardt/tiktok-mcp/commit/2af5451c14773c8394da96fa783fdc23e5e7deb4))
* **auth:** surface TikTok OAuth token-exchange error envelopes + correct PKCE encoding ([1070aac](https://github.com/sigvardt/tiktok-mcp/commit/1070aac4572a514b93b2b95793b85dfa21905005))
* **auth:** use TikTok Desktop hex PKCE challenge ([8579b74](https://github.com/sigvardt/tiktok-mcp/commit/8579b74bac4a968cc71362c2c3d059d648ad692d))
* **build:** set pyproject authors email to [email protected] ([e8ee7bf](https://github.com/sigvardt/tiktok-mcp/commit/e8ee7bf629b3d09c73e2b6ff4920c7ebecf46c13))
* **business:** keep pre-minted marketing auth errors repairable ([2ff45df](https://github.com/sigvardt/tiktok-mcp/commit/2ff45df441a0ac2df8a53e1dc9421bf9c9f56c59))
* **comments:** use organic account OAuth for reads ([7efd179](https://github.com/sigvardt/tiktok-mcp/commit/7efd17950088afd250d793fd5183aca02dbddff4))
* **display:** POST + fields query param for v2 read endpoints ([fc818d8](https://github.com/sigvardt/tiktok-mcp/commit/fc818d8ea6bc70eb9cad67a920975eb3015e916f))
* **marketing:** add schedule_start_time + schedule_end_time to create_adgroup ([e8812a7](https://github.com/sigvardt/tiktok-mcp/commit/e8812a740be1817ef6c9d564a823eab329d69f1f))
* **marketing:** correct list_advertisers endpoint behavior ([8dfe7a5](https://github.com/sigvardt/tiktok-mcp/commit/8dfe7a5b04a0211eb845f4f77d63b6024bee2f62))
* **marketing:** graceful envelope for sandbox-unavailable BC endpoints ([a1432dc](https://github.com/sigvardt/tiktok-mcp/commit/a1432dc87fda4d2cf44349aeac4d011cb1008eba))
* **marketing:** route delete_campaign through status/update endpoint with DELETE ([9fa7710](https://github.com/sigvardt/tiktok-mcp/commit/9fa7710f1dcda11d4ec57ac10b89333a53c18006))
* **posting:** graceful envelope for unknown publish_id on status_fetch ([e7a1faf](https://github.com/sigvardt/tiktok-mcp/commit/e7a1faf6b632ea54e02d09d8a8cafaaaba6178f6))
* **posting:** make CreatorInfo permissive to live TikTok response shape ([1dd23a3](https://github.com/sigvardt/tiktok-mcp/commit/1dd23a3b39832539173cd2562ce457615bc3c2da))
* **posting:** wire draft tools into client and registry ([f42374c](https://github.com/sigvardt/tiktok-mcp/commit/f42374c2ab68ced150160952f9c9d122ddaf2469))
* **server:** pass package version to FastMCP constructor ([a754f1c](https://github.com/sigvardt/tiktok-mcp/commit/a754f1c04650978c5ff5a9db1a621088fc1a1352))
* **tools/comments:** use verified Business Comment endpoints ([2b41acf](https://github.com/sigvardt/tiktok-mcp/commit/2b41acf66481e03e433822223e76c138c0b99194))


### Infrastructure

* create src-layout package skeleton with tests/ and placeholder modules ([55ac2cc](https://github.com/sigvardt/tiktok-mcp/commit/55ac2cc82ed01bbf67640882af0268999261e176))
* fix release validation across platforms ([35d6695](https://github.com/sigvardt/tiktok-mcp/commit/35d6695e8b0006d4ea92dd9f6139c2c0de39e9b7))
* lint + type + 9-cell test matrix + smoke + docs jobs ([0fd86a2](https://github.com/sigvardt/tiktok-mcp/commit/0fd86a26b3e642999cccc935ad1e16a71651fb12))
* release-please CHANGELOG automation + release PR flow ([cac4e2c](https://github.com/sigvardt/tiktok-mcp/commit/cac4e2c0103a8db3c1af8d0e80f615336e4429c8))
* **release:** finalize PyPI publishing setup ([192b5bb](https://github.com/sigvardt/tiktok-mcp/commit/192b5bbbdc280e9a145201fe3f41cdc46f718235))
* **release:** OIDC PyPI publish on v* tags (job-level id-token, env: pypi) ([d3ac6a5](https://github.com/sigvardt/tiktok-mcp/commit/d3ac6a5a51ac87badc6753e2c420b271859ecfe7))
* scaffold pyproject.toml with hatchling + hatch-vcs and core deps ([928799a](https://github.com/sigvardt/tiktok-mcp/commit/928799ab781b0999e05f06565f71095d319bf58f))
* **smoke:** post-release distribution validation across 9 cells ([aa664bd](https://github.com/sigvardt/tiktok-mcp/commit/aa664bda5ec8926f7a4125a0086f041f938a0b69))


### Documentation

* auth architecture design doc ([697a49e](https://github.com/sigvardt/tiktok-mcp/commit/697a49e827eddfc1ddd326af318545d186e295cb))
* **display:** record Display read request shape ([66ad878](https://github.com/sigvardt/tiktok-mcp/commit/66ad8789ab8df03ac9195217e5eb7cf0ea3e211c))
* **inventory:** enumerate v0.1 API surface across Display, Marketing, Business Organic, Content Posting ([9718d91](https://github.com/sigvardt/tiktok-mcp/commit/9718d91feb41c87a710a6a2296b576be63156335))
* PyPI pending-publisher bootstrap runbook ([dd4eb8c](https://github.com/sigvardt/tiktok-mcp/commit/dd4eb8ca4bf59e5e09ab652c6f6a46bee7390c0e))
* **readme:** user-facing README with claude_desktop_config examples ([53c0419](https://github.com/sigvardt/tiktok-mcp/commit/53c04199d81be63a06e944535cfaf83af1913e8b))
* refresh README and scrub examples ([3728d2c](https://github.com/sigvardt/tiktok-mcp/commit/3728d2c09bb686eeb392c086882ad6e94548968e))
* release runbook + hotfix flow ([3fdfebd](https://github.com/sigvardt/tiktok-mcp/commit/3fdfebda76db2b095eb53bcacd7f5a7d5420dc1e))
* security model + threat-to-defense matrix ([b217af0](https://github.com/sigvardt/tiktok-mcp/commit/b217af0fd10e9adc4b94b17fa9b481a12aba30f2))


### Tests

* **decorators:** drop obsolete dual-env-var tests superseded by TIKTOK_MCP_LIVE_ACCOUNT_SAFETY ([5995690](https://github.com/sigvardt/tiktok-mcp/commit/59956902f2125805385639cb141140c3fb1d4ad1))
* **lint:** tool-name + annotation inventory audit ([d08bb3a](https://github.com/sigvardt/tiktok-mcp/commit/d08bb3aa1b7a0bb78687258e1c47fb0db1e95b99))


### Chores

* **plan:** mark F1-F4 wave APPROVE and T42 as BLOCKED EXTERNAL ([b7e68a0](https://github.com/sigvardt/tiktok-mcp/commit/b7e68a04da333a26e92b3ae329adeb8d46cdd1b9))
* **quality:** document intentional conventions and annotate suppressions for F-wave review ([a5e58c0](https://github.com/sigvardt/tiktok-mcp/commit/a5e58c04bbdb28dda9bd7e71e2269ce6e095fcc6))
* **spike:** draft Wave-0 S1/S2/S3 spike scripts and PyPI publish workflow ([99ff3bb](https://github.com/sigvardt/tiktok-mcp/commit/99ff3bb38507dbc0d9bf965a903cf1cc9a79e9d3))

## [Unreleased]

## 0.1.1 (2026-05-27)

### Bug Fixes

- Persist `redirect_uri` from `set_app_credentials` so account onboarding can build OAuth URLs after credentials are saved.
- Preserve exact loopback redirect ports during local OAuth capture and fall back to the manual URL when the registered port is unavailable.
