![CI](https://github.com/signikant/tiktok-mcp/actions/workflows/ci.yml/badge.svg)
<!-- markdownlint-disable MD013 MD034 -->

# tiktok-mcp

TikTok MCP: read your TikTok organic and ad performance, comments, and post content. Multi-account, multi-API, uvx-distributed.

`tiktok-mcp` is a Model Context Protocol server that lets Claude Desktop (or any MCP client) talk to four TikTok surfaces from one process:

- **Display API**: user info, video list, post insights
- **Marketing API**: campaigns, ad groups, ads, reports, audiences, creatives
- **Business Organic API**: comments on your own videos plus reply moderation
- **Content Posting API**: video and photo uploads (drafts by default)

It ships 40+ MCP tools, 2 MCP resources (`tiktok-mcp://accounts/`, `tiktok-mcp://app-credentials/`), and 3 prompt templates (`weekly_marketing_report`, `comment_queue_triage`, `weekly_engagement_summary`). Tokens stay in your OS keychain. No hosted relay, no cloud, no telemetry.

## Quick start

1. Smoke-test the package without installing anything:

   ```bash
   uvx tiktok-mcp@0.1.0 --version
   ```

2. Add the `claude_desktop_config.json` snippet for your operating system (see [claude_desktop_config.json examples](#claude_desktop_configjson-examples) below).

3. Restart Claude Desktop and ask it to list available `tiktok-mcp` tools to confirm the server booted.

## Supported APIs

| API | Surface | Read / Write / Both |
| --- | --- | --- |
| Display | user info, video list, post insights | Both (revoke is the only write today) |
| Marketing | campaigns, ad groups, ads, reports, audience uploads, creative uploads | Both |
| Business Organic | own-video comments and reply moderation | Both |
| Content Posting | video and photo upload, draft inbox, publish status | Both (drafts default; direct posting is opt-in) |

> Direct publishing is off by default. Content Posting tools land assets in your TikTok draft inbox unless you pass `publish_immediately=True` explicitly on the relevant upload tool. The mobile app stays the publish UI for the default flow.

## claude_desktop_config.json examples

The Claude Desktop config file lives at:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Each example below pins the same version (`tiktok-mcp@0.1.0`), sets a conservative writes posture (account-change tools enabled so first-time onboarding works, no general TikTok-side writes yet), and turns on structured stderr logging. Copy the block matching your operating system into the config file above.

### macOS

```json
{
  "mcpServers": {
    "tiktok-mcp": {
      "command": "uvx",
      "args": ["tiktok-mcp@0.1.0"],
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

### Windows

```json
{
  "mcpServers": {
    "tiktok-mcp": {
      "command": "uvx",
      "args": ["tiktok-mcp@0.1.0"],
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

### Linux

```json
{
  "mcpServers": {
    "tiktok-mcp": {
      "command": "uvx",
      "args": ["tiktok-mcp@0.1.0"],
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

If `uvx` is not on your `PATH`, install it with `pipx install uv` (or follow the [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/)) and re-run the smoke command. As an alternative, replace `"command": "uvx"` with the absolute path to `uvx` (output of `which uvx` on macOS/Linux or `where uvx` on Windows). The `tiktok-mcp` console script is also installed when you `uv pip install tiktok-mcp`, but the `uvx` form keeps the install ephemeral and pinned.

## First-time setup walkthrough

Claude drives onboarding through MCP tool calls. You do not edit any files by hand after the config snippet above.

1. Restart Claude Desktop so it picks up the new `tiktok-mcp` server entry.

2. Tell Claude: "Set my TikTok Marketing API app credentials." Claude will call the `set_app_credentials` tool with the `client_id`, `client_secret`, and registered `redirect_uri` you supply. Use the same `redirect_uri` you registered on the TikTok developer console; the placeholder used throughout these examples is `https://oauth.example.com`.

3. Tell Claude: "Add a Marketing account aliased `demo-marketing`." Claude will call `add_account(api_type="marketing", alias_hint="demo-marketing")`. The tool returns an authorization URL plus a short-lived `state` token (10-minute TTL).

4. Open the authorization URL in any browser, sign in to TikTok, and approve the requested scopes. TikTok redirects your browser to your registered URI, for example `https://oauth.example.com?code=...&state=...`.

5. Copy the full redirect URL out of the address bar and paste it back to Claude. Claude calls `complete_account_login(redirect_url=...)`, which validates the state, swaps the code for tokens, and writes them atomically to your OS keychain.

6. Repeat for any other API surface (Display, Business Organic, Content Posting) by passing the matching `api_type` value.

Tokens never leave your machine. The MCP server has no hosted component; the redirect target is a static `https://` URL you control on the TikTok developer console.

## Writes opt-in

`tiktok-mcp` ships with writes off. Every mutation tool is annotated with `destructiveHint: true` (so Claude Desktop prompts per call) and decorated with `@require_writes_enabled("<api>")`, which checks `TIKTOK_MCP_ALLOW_WRITES` at every invocation. Toggling the env var mid-session therefore takes effect on the next call without a restart.

`TIKTOK_MCP_ALLOW_WRITES` accepts:

| Value | Behaviour |
| --- | --- |
| (unset), `""`, `"0"`, `"false"`, `"no"` | All writes blocked |
| `"1"`, `"true"`, `"yes"`, `"all"` | All writes enabled |
| `"marketing"` | Only Marketing API writes |
| `"comments"` | Only Business Organic comment moderation writes |
| `"display"` | Only Display API writes |
| `"posting"` | Only Content Posting writes |
| `"marketing,comments"` (any comma-separated subset of `marketing,comments,display,posting`) | Enables the listed surfaces only |

Account add/remove/rename and app-credential management are gated separately by `TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES` (binary: unset, `0`, `false`, or `no` blocks; `1`, `true`, or `yes` enables). You typically want this on during onboarding and off afterwards.

When a write is blocked, the tool returns a structured error envelope instead of failing silently:

```json
{
  "error": "writes_disabled",
  "message": "Write/delete tools for 'marketing' are disabled. Set TIKTOK_MCP_ALLOW_WRITES=all (or include 'marketing') to enable.",
  "tool": "marketing_update_campaign_status",
  "api": "marketing",
  "would_have_done": "Pause campaign 1700000000000000001 on account demo-marketing"
}
```

`TIKTOK_MCP_ALLOW_LIVE_WRITES` is a separate test-only gate. CI never sets it, and you do not need to set it for normal use; it only opts a local `pytest` run into hitting TikTok's live write endpoints. Tests tagged `@pytest.mark.live_write` are auto-skipped without it.

## Sandbox

TikTok exposes a sandbox tenant for Marketing API testing. To route every keychain entry, OAuth flow, and HTTP call through sandbox semantics, set `TIKTOK_MCP_USE_SANDBOX=1` in the server's env block:

```json
{
  "mcpServers": {
    "tiktok-mcp-sandbox": {
      "command": "uvx",
      "args": ["tiktok-mcp@0.1.0"],
      "env": {
        "TIKTOK_MCP_USE_SANDBOX": "1",
        "TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES": "1",
        "TIKTOK_MCP_ALLOW_WRITES": "all",
        "TIKTOK_MCP_LOG_LEVEL": "DEBUG"
      }
    }
  }
}
```

Sandbox accounts live in a separate keychain namespace (`tiktok-mcp::<api>::sandbox::<alias>`) from production accounts (`tiktok-mcp::<api>::production::<alias>`). The two namespaces are mutually exclusive: a sandbox token cannot be used by a production tool call, and vice versa. This stops accidental cross-tenant calls in mixed environments.

The recommended workflow is to keep `tiktok-mcp` and `tiktok-mcp-sandbox` as side-by-side entries in `claude_desktop_config.json` so production and sandbox servers run together without colliding.

## Security

### Token storage

Tokens are written to the OS keychain via the [`keyring`](https://pypi.org/project/keyring/) library:

- macOS: Keychain (login keychain by default)
- Windows: Credential Manager
- Linux: Secret Service (GNOME Keyring, KWallet, or any libsecret backend)

When no keychain backend is available (a headless Linux container without a running Secret Service, for example), `tiktok-mcp` falls back to an AES-encrypted JSON file under `platformdirs.user_data_dir("tiktok-mcp")`:

- macOS: `~/Library/Application Support/tiktok-mcp/`
- Windows: `%LOCALAPPDATA%\tiktok-mcp\`
- Linux: `~/.local/share/tiktok-mcp/`

Encryption uses `cryptography.fernet` (AES-128-CBC + HMAC-SHA-256). The fernet key itself is stored in keychain whenever a keychain backend is reachable. Plain unencrypted token files are never an acceptable fallback.

### Refresh-token rotation

Refresh-token rotation is atomic. The new refresh token is written to keychain before the old one is discarded, under a per-account `asyncio.Lock` so concurrent expired-token requests deduplicate to a single refresh round-trip. If the keychain write fails, the old tokens are retained in memory and the call raises instead of silently dropping the only working refresh token.

### Redaction

A `SecretRedactor` logging filter is installed on the root logger and cannot be disabled. It rewrites any `access_token=`, `refresh_token=`, `code=`, `client_secret=`, or `Authorization:` value to `<REDACTED:token_name>`. Every token read out of keychain is added to a runtime redaction set, so even custom log lines you write through the standard `logging` package stay redacted. `httpx` exceptions are wrapped to strip response bodies before bubbling up.

Comment text is never persisted, never cached, and never logged at INFO. Dumping bodies for debugging requires both `TIKTOK_MCP_LOG_LEVEL=DEBUG` and `TIKTOK_MCP_LOG_COMMENT_BODIES=1`; either one on its own keeps comment bodies out of logs.

### Logging knobs

| Env var | Default | Effect |
| --- | --- | --- |
| `TIKTOK_MCP_LOG_LEVEL` | `INFO` | Sets the level on the root logger |
| `TIKTOK_MCP_LOG_FILE` | unset (stderr only) | Mirrors stderr output to this file path |
| `TIKTOK_MCP_LOG_FORMAT` | text | Use `json` for one-line-per-record structured logs |
| `TIKTOK_MCP_LOG_COMMENT_BODIES` | unset | `1` plus DEBUG level required to dump comment bodies |

## Troubleshooting

### Keyring backend not available on Linux

Symptom: `tiktok-mcp` boots but tools complain that no keyring backend is available and the encrypted-file fallback kicks in (the path appears in stderr at startup).

Fix: install a Secret Service implementation and unlock it once.

```bash
sudo apt install gnome-keyring libsecret-tools
gnome-keyring-daemon --unlock
```

If you cannot install one (for example on a restricted CI host), the encrypted JSON file at `~/.local/share/tiktok-mcp/` is a supported fallback. Keep that directory backed up and on an encrypted disk.

### `state mismatch` or `state expired` on `complete_account_login`

Symptom: pasting the redirect URL produces an error envelope containing `"error": "state_mismatch"` or `"error": "state_expired"`.

Cause: OAuth state lives in memory only with a 10-minute TTL. Either the MCP server restarted between `add_account` and `complete_account_login`, or you waited too long to paste back.

Fix: re-run `add_account` to get a fresh URL and finish the paste within 10 minutes.

```text
"Add a Marketing account aliased demo-marketing."
```

### `redirect host mismatch` on `complete_account_login`

Symptom: the error envelope contains `"error": "redirect_host_mismatch"`.

Cause: the redirect URL you pasted does not match the `redirect_uri` you registered on the TikTok developer console. The host check is exact; subdomains and trailing slashes count.

Fix: confirm the registered URI (the examples use `https://oauth.example.com`) and re-call `set_app_credentials` with the correct value, then start a fresh `add_account` flow.

```text
"Update my Marketing API app credentials. The redirect URI is https://oauth.example.com."
```

### `writes_disabled` when calling a write tool

Symptom: a mutation tool returns the `writes_disabled` envelope documented above.

Fix: widen `TIKTOK_MCP_ALLOW_WRITES` to include the API surface you need, then restart Claude Desktop (or just re-issue the tool call; the gate is re-checked per invocation). For example, to enable Marketing and Comments writes:

```json
{
  "env": {
    "TIKTOK_MCP_ALLOW_WRITES": "marketing,comments"
  }
}
```

The error envelope's `would_have_done` field describes the blocked operation in plain English, so you can decide whether to widen the gate or stick with the read-only equivalent.

### `tiktok-mcp` tools not appearing in Claude Desktop

Symptom: Claude Desktop has restarted but the `tiktok-mcp` tools list is empty.

Fix: confirm the server is actually launching.

```bash
uvx tiktok-mcp@0.1.0 --version
```

If that prints a version, check the Claude Desktop MCP log on your platform (macOS: `~/Library/Logs/Claude/`, Windows: `%APPDATA%\Claude\logs\`, Linux: `~/.config/Claude/logs/`). The most common cause is a syntax error in `claude_desktop_config.json`; an unescaped backslash in the Windows redirect path is the usual culprit. Validate the JSON locally:

```bash
python -m json.tool < claude_desktop_config.json
```

## Roadmap

These surfaces are intentionally out of scope for v0.1 and tracked for v0.2:

- Marketing Catalog Manager and Dynamic Product Ads
- Custom Audience segments and lookalike modelling
- Reservation buying (v0.1 is auction only)
- Pixel and Events API
- Business Organic comment search across other creators
- Interactive Content Posting features (slideshows, polls, interactive add-ons)
- Long-term analytics storage and a web UI

Track progress on the [GitHub issues page](https://github.com/signikant/tiktok-mcp/issues).

## License

MIT. See [LICENSE](LICENSE) for the full text.

## Contributing

Bug reports, reproductions, and pull requests are welcome at [github.com/signikant/tiktok-mcp](https://github.com/signikant/tiktok-mcp). A few ground rules:

1. Open an issue first for anything beyond a documentation typo so we can agree on shape before code lands.
2. Run `uv run pytest -q` locally and confirm green before pushing. The integration suite uses `vcrpy` replay only by default, so it should pass without TikTok credentials.
3. Keep cassettes scrubbed. The project's `vcrpy` config strips `Authorization`, `Access-Token`, `client_secret`, and any `text` or `comment_text` body field, but a manual `grep -r "Bearer " tests/cassettes/` before commit is the easy backstop.

For questions or commercial support, contact `[email protected]`.
