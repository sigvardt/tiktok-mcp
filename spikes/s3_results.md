# S3 vcrpy Fidelity Spike Results

## Implementation Notes

- vcrpy version chosen: `8.1.1` resolved by `uvx --with vcrpy python -c "import vcr; print(vcr.__version__)"` on 2026-05-22. Operator should fill the exact local recording version if different: `<version>`
- Supporting packages: `pytest`, `httpx`
- Install/run command: `uvx --with vcrpy --with pytest --with httpx pytest spikes/test_s3_vcr.py -v`
- Scrub config used: `filter_headers=[("Access-Token", "REDACTED"), ("Authorization", "REDACTED"), ("X-Tt-Access-Token", "REDACTED")]` plus `before_record_response=scrub_response_body`
- TikTok quirk: Business API uses `Access-Token` header, not the usual `Authorization: Bearer ...` header.
- Unexpected HTTP 4xx guidance: if the invalid advertiser/info call returns HTTP 4xx instead of HTTP 200 + `code != 0`, record that here as a separate finding before changing the spike shape.

## Cassette Recording

- Cassette path: `spikes/cassettes/s3_business_error.yaml`
- Recording date: `<YYYY-MM-DD>`
- Recording mode used: `once`
- Command used: `uvx --with vcrpy --with pytest --with httpx pytest spikes/test_s3_vcr.py::test_record -v`
- Token scrub verification:
  - `grep -E "Bearer [A-Za-z0-9_-]{20,}" spikes/cassettes/s3_business_error.yaml` result: `<no matches / details>`
  - `grep -E "access_token['\"]?\s*:\s*['\"][A-Za-z0-9_-]{20,}" spikes/cassettes/s3_business_error.yaml` result: `<no matches / details>`
  - `python -c "from spikes.s3_vcr import verify_cassette_no_leaks; assert verify_cassette_no_leaks('spikes/cassettes/s3_business_error.yaml')"` result: `<pass / fail>`

## Decoder Pattern

This minimal prototype is the contract Wave 1 T7 should codify in `src/tiktok_mcp/envelopes.py`:

```python
def decode_business_response(body: dict) -> dict:
    """Decode HTTP 200 + code != 0 envelope per TikTok Business API; mirror in Wave 1 T7."""
    if body["code"] != 0:
        raise BusinessApiError(
            code=body["code"],
            message=body.get("message", ""),
            request_id=body.get("request_id"),
        )
    return body.get("data", body)
```

## Determinism Check

- Replay command: `uvx --with vcrpy --with pytest --with httpx pytest spikes/test_s3_vcr.py::test_replay -v`
- 10x replay command: `for i in $(seq 1 10); do uvx --with vcrpy --with pytest --with httpx pytest spikes/test_s3_vcr.py::test_replay -v || break; done`
- 10/10 pass result: `<pass / fail>`
- Observed deterministic tuple `(code, message, request_id)`: `<tuple>`

## Gotchas

- `<gotcha 1>`
- `<gotcha 2>`
- `<gotcha 3>`

## DECISION: <PASS|PARTIAL|FAIL>
