# S1 Redirect URL Fidelity Results

Operator fills this file after running the manual-paste OAuth flow against TikTok sandbox apps only.

## How To Run

Set the six sandbox env vars first:

```bash
export TIKTOK_S1_DISPLAY_APP_ID='<sandbox display app id>'
export TIKTOK_S1_DISPLAY_CLIENT_SECRET='<sandbox display client secret>'
export TIKTOK_S1_DISPLAY_REDIRECT_URI='<registered display redirect uri>'
export TIKTOK_S1_BUSINESS_APP_ID='<sandbox business app id>'
export TIKTOK_S1_BUSINESS_CLIENT_SECRET='<sandbox business client secret>'
export TIKTOK_S1_BUSINESS_REDIRECT_URI='<registered business redirect uri>'
```

Run Display:

```bash
python spikes/s1_redirect.py --api display --action open --no-browser
python spikes/s1_redirect.py --api display --action exchange --url '<PASTE_FULL_REDIRECT_URL>'
```

Run Business:

```bash
python spikes/s1_redirect.py --api business --action open --no-browser
python spikes/s1_redirect.py --api business --action exchange --url '<PASTE_FULL_REDIRECT_URL>'
```

Do not paste full codes, tokens, client secrets, or screenshots containing them into this file.

## Implementation Notes

- HTTP client choice: this spike uses stdlib `urllib.request` synchronously so the operator can run it without adding `httpx` or editing `pyproject.toml`.
- Display API uses form-encoded token exchange with PKCE S256. The script stores only state and PKCE verifier in the OS temp directory between `open` and `exchange`; it never writes OAuth codes or tokens to disk.
- Business API token exchange intentionally uses JSON, not form-encoded data. This is the TikTok Business API divergence the spike must validate.
- Browser launch is fire-and-forget. Use `--no-browser` when running over SSH or in a headless shell.
- The parser accepts bare URLs, whitespace, surrounding double quotes, surrounding backticks, markdown links, and trailing newlines.

## Display API

- registered_uri: `<fill in registered sandbox redirect URI>`
- captured_url_pattern: `<fill in redacted shape, e.g. https://host/path?code=ABCD...(len=N)&state=WXYZ...(len=N)>`
- captured_host_matches_registered_uri: `<true|false>`
- code_param_present: `<true|false>`
- state_param_preserved: `<true|false>`
- params_preserved: `<list keys preserved, not values>`
- params_lost: `<list expected keys missing, or none>`
- extra_params: `<list unexpected keys, or none>`
- token_exchange: `<true|false>`
- token_exchange_response_shape: `<redacted keys only, e.g. access_token, refresh_token, expires_in>`
- verdict: PASS/PARTIAL/FAIL `<choose one and explain>`
- implementation_notes: `<operator notes for Wave 2 complete_account_login parser>`

## Business API

- registered_uri: `<fill in registered sandbox redirect URI>`
- captured_url_pattern: `<fill in redacted shape, e.g. https://host/path?auth_code=ABCD...(len=N)&state=WXYZ...(len=N)>`
- captured_host_matches_registered_uri: `<true|false>`
- code_or_auth_code_param_present: `<true|false>`
- state_param_preserved: `<true|false>`
- params_preserved: `<list keys preserved, not values>`
- params_lost: `<list expected keys missing, or none>`
- extra_params: `<list unexpected keys, or none>`
- token_exchange: `<true|false>`
- token_exchange_response_shape: `<redacted keys only, e.g. code, message, data.access_token>`
- verdict: PASS/PARTIAL/FAIL `<choose one and explain>`
- implementation_notes: `<operator notes for Business API JSON token exchange and parser behavior>`

## Overall Assessment

- manual_paste_flow_viable: `<true|false>`
- implementation_impact: `<proceed|proceed with parser notes|halt and revisit auth design>`
- evidence_location: `<screenshots or Playwright codegen recording path, if captured>`
- sanitized_stdout_location: `<optional path to redacted terminal transcript, if captured>`

## DECISION: <fill in PASS|PARTIAL|FAIL>
