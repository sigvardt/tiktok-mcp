![CI](https://github.com/sigvardt/tiktok-mcp/actions/workflows/ci.yml/badge.svg)
<!-- markdownlint-disable MD013 MD034 -->

# tiktok-mcp

`tiktok-mcp` is a local Model Context Protocol server for working with TikTok
Display, Marketing, Business Organic, and Content Posting APIs from Claude
Desktop or another MCP client.

The server runs on your machine, stores OAuth tokens in your OS keychain, and has
no hosted relay or telemetry. It supports multi-account use across production and
sandbox namespaces, with write tools blocked unless you explicitly opt in.

## What It Supports

| Surface | Main Use | Current Tooling |
| --- | --- | --- |
| Display API | User info, video list, video metrics | Read tools plus token revoke/refresh utilities |
| Marketing API | Advertisers, campaigns, ad groups, ads, reports, creatives, audiences | Read tools and gated write tools |
| Business Organic Accounts API | Comments on owned videos and comment moderation | Read tools and gated moderation writes |
| Content Posting API | Video/photo upload, drafts, publish status | Draft-first upload tooling and gated posting writes |

MCP resources:

- `tiktok-mcp://accounts/`
- `tiktok-mcp://app-credentials/`

Prompt templates:

- `weekly_marketing_report`
- `comment_queue_triage`
- `weekly_engagement_summary`

## Install

Smoke-test the package:

```bash
uvx tiktok-mcp --version
```

For local development from this repository:

```bash
uv run tiktok-mcp --version
```

## Claude Desktop

Claude Desktop config locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Minimal read-first config:

```json
{
  "mcpServers": {
    "tiktok-mcp": {
      "command": "uvx",
      "args": ["tiktok-mcp"],
      "env": {
        "TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES": "1",
        "TIKTOK_MCP_ALLOW_WRITES": "",
        "TIKTOK_MCP_LOG_LEVEL": "INFO",
        "TIKTOK_MCP_LOG_FORMAT": "json"
      }
    }
  }
}
```

Restart Claude Desktop after changing this file.

## First-Time Setup

Setup is done through MCP tools. You do not need to hand-edit token files.

1. Ask Claude to set app credentials for the API surface you want, for example:
   `set_app_credentials(api_type="business_organic", client_id=..., client_secret=..., redirect_uri=...)`.

2. Ask Claude to add an account:
   `add_account(api_type="business_organic", alias="comments-live")`.

3. Open the returned authorization URL, approve TikTok access, and copy the full
   redirected URL from the browser address bar.

4. Paste that full redirect URL back to Claude. Claude calls
   `complete_account_login(...)`, validates the OAuth state, exchanges the code,
   and stores the resulting account tokens in keychain.

5. Turn account changes off after setup by unsetting
   `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` or setting it to `0`.

Account aliases are local names. Tokens and app secrets are never returned by
normal listing tools; account IDs are exposed only as fingerprints where possible.

If the registered redirect URI is a local loopback URL such as
`http://localhost:8765/callback`, Claude can use `add_account(...,
await_callback=true)` or `add_account_with_loopback(...)`. Those tools return a
`pending` response immediately and Claude should call `poll_loopback_login(state,
wait_seconds=...)` until the browser redirect is captured. For hosted redirect
URIs, use the manual paste flow above.

## OAuth Notes

Each TikTok surface has its own OAuth details:

| API Type | Authorization Flow | Token Endpoint |
| --- | --- | --- |
| `display` | TikTok v2 account OAuth with PKCE | `https://open.tiktokapis.com/v2/oauth/token/` |
| `content_posting` | TikTok v2 account OAuth with PKCE | `https://open.tiktokapis.com/v2/oauth/token/` |
| `marketing` | TikTok For Business advertiser authorization | `business-api.tiktok.com` + `/open_api/v1.3/oauth2/access_token/` |
| `business_organic` | TikTok account-holder authorization | `business-api.tiktok.com` + `/open_api/v1.3/tt_user/oauth2/token/` |

Stored credentials must include the exact redirect URI registered with TikTok.
Legacy credentials without a redirect URI must be re-saved with
`set_app_credentials(..., redirect_uri=...)` before account onboarding.
Sandbox Business API accounts still exchange OAuth codes through
`business-api.tiktok.com`; the `sandbox` flag only changes the downstream
Business API resource host used after tokens are stored.

Marketing token responses from TikTok For Business may omit `expires_in`.
`tiktok-mcp` applies TikTok's documented 24-hour marketing access-token
lifetime and stores the refresh-token lifetime from `refresh_token_expire_in`.

Business Organic comment reads require the TikTok account-holder flow, not the
Marketing advertiser flow. The default requested scopes are:

- `user.info.basic`
- `video.list`
- `comment.list`
- `comment.list.manage`

The stored Organic account ID is the `open_id` returned by the `/tt_user` token
flow. Comment read tools use that ID as `business_id` automatically.

## Comment Reads

Business Organic read tools are read-only MCP tools:

- `comments_list(alias, video_id, business_id=None, cursor=0, max_count=20, ...)`
- `comments_list_replies(alias, video_id, comment_id, business_id=None, cursor=0, max_count=20, ...)`

`business_id` is optional for normal use. If omitted, the tool uses the stored
Organic account ID for the alias.

Typical flow:

1. Use a known owned TikTok `video_id`, or discover one with the Accounts API
   video list endpoint.
2. Call `comments_list(...)` to fetch comments on the owned video.
3. Pick a top-level `comment_id`.
4. Call `comments_list_replies(...)` to fetch replies for that comment.

For local E2E verification, this repo includes a read-only smoke runner:

```bash
TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES=1 \
  uv run python spikes/live_comments_read_e2e.py --alias comments-live-e2e --oauth
```

The runner performs OAuth, discovers an owned video through read-only
`GET /business/video/list/`, then calls:

- `GET /business/comment/list/`
- `GET /business/comment/reply/list/`

It prints counts and ID fingerprints only. It does not print comment bodies or
OAuth tokens.

## Write Safety

There are two gates for TikTok-side writes.

`TIKTOK_MCP_ALLOW_WRITES` controls which write namespaces may run:

| Value | Effect |
| --- | --- |
| unset, `""`, `0`, `false`, `no` | Block all writes |
| `marketing` | Enable Marketing writes |
| `comments` | Enable Business Organic moderation writes |
| `posting` | Enable Content Posting writes |
| `display` | Enable Display token write utilities |
| `all`, `1`, `true`, `yes` | Enable every write namespace |
| comma-separated list | Enable only those namespaces |

`TIKTOK_MCP_LIVE_ACCOUNT_SAFETY` is an additional live-account lock. When unset,
it locks all destructive live API surfaces even if `TIKTOK_MCP_ALLOW_WRITES` is
set. To intentionally unlock writes for a live session, set it explicitly:

```json
{
  "env": {
    "TIKTOK_MCP_ALLOW_WRITES": "comments",
    "TIKTOK_MCP_LIVE_ACCOUNT_SAFETY": ""
  }
}
```

Keep both unset or empty for read-only use.

Account inventory changes are separate. `set_app_credentials`, `add_account`,
`complete_account_login`, `rename_account`, and `remove_account` are gated by
`TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES`, not by `TIKTOK_MCP_ALLOW_WRITES`.

## Sandbox

TikTok’s sandbox support is primarily useful for Marketing API testing. Sandbox
accounts are stored separately from production accounts:

- production: `tiktok-mcp::<api>::production::<alias>`
- sandbox: `tiktok-mcp::<api>::sandbox::<alias>`

Pass `sandbox=true` when adding or using sandbox accounts. A sandbox token is not
used for production calls, and a production token is not used for sandbox calls.

## Token Storage And Logging

Tokens are stored through the Python `keyring` library:

- macOS: Keychain
- Windows: Credential Manager
- Linux: Secret Service

If keyring is unavailable, the server falls back to an encrypted file under the
platform user-data directory. Plain token files are not used.

Logging protections:

- Access tokens, refresh tokens, auth codes, client secrets, and authorization
  headers are redacted.
- Comment bodies are not logged at INFO.
- Raw comment-body logging requires both `TIKTOK_MCP_LOG_LEVEL=DEBUG` and
  `TIKTOK_MCP_LOG_COMMENT_BODIES=1`.

Useful logging env vars:

| Env Var | Default | Effect |
| --- | --- | --- |
| `TIKTOK_MCP_LOG_LEVEL` | `INFO` | Root log level |
| `TIKTOK_MCP_LOG_FORMAT` | text | Use `json` for structured one-line logs |
| `TIKTOK_MCP_LOG_FILE` | unset | Mirror logs to a file |
| `TIKTOK_MCP_LOG_COMMENT_BODIES` | unset | Opt in to DEBUG comment-body logging |

## Development

Install dependencies and run the focused checks:

```bash
uv run --extra test pytest
uv run --extra dev ruff check .
uv run --extra dev mypy src/tiktok_mcp
```

Validate README JSON blocks:

```bash
uv run python tests/docs/validate_readme_json.py
```

Default tests use mocked transports or scrubbed replay cassettes. Do not run live
write tests against a production account unless you intentionally configured both
write gates and understand the operation.

## Troubleshooting

### Account tools return `account_changes_disabled`

Set `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES=1`, restart the MCP server, and retry the
setup action. Turn it off again after onboarding.

### OAuth state expired or invalid

OAuth state is in memory and valid for about 10 minutes. Start a fresh
`add_account` flow and paste the redirected URL back before the state expires.

### Comment reads return account-not-found

Confirm you added a `business_organic` account, not a Marketing account. Comment
reads require the TikTok account-holder OAuth flow and the stored Organic
`open_id`.

### Comment video discovery returns empty pages

The Accounts API can return sparse video-list pages. Paginate by passing the
returned cursor while `has_more=true`, or provide a known owned `video_id`
directly to `comments_list`.

### A write tool returns `live_account_safety_locked`

`TIKTOK_MCP_LIVE_ACCOUNT_SAFETY` is still locking that namespace. Leave it locked
for read-only work. Unlock only the specific session where you intend to mutate
TikTok state.

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Bug reports and pull requests are welcome. Keep test cassettes scrubbed, avoid
committing live IDs or OAuth redirects, and run the development checks before
pushing.
