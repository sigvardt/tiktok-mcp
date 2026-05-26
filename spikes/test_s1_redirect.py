from __future__ import annotations

import base64
import hashlib
import json
import unittest
import urllib.parse
import urllib.request
from typing import cast
from unittest.mock import patch

from spikes import s1_redirect


class FakeResponse:
    def __init__(self, payload: dict[str, s1_redirect.JsonValue], status: int = 200) -> None:
        self.payload: dict[str, s1_redirect.JsonValue] = payload
        self.status: int = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def close(self) -> None:
        return None


class RedirectParserTests(unittest.TestCase):
    def test_parse_redirect_url_handles_required_plan_shapes(self) -> None:
        test_url = "https://oauth.example.com/?code=ABC&state=XYZ"
        cases = [
            test_url,
            f"  {test_url}  ",
            f'"{test_url}"',
            f"`{test_url}`",
            f"[click here]({test_url})",
            f"{test_url}\n",
        ]

        for raw_url in cases:
            with self.subTest(raw_url=raw_url):
                parsed = s1_redirect.parse_redirect_url(raw_url)
                self.assertEqual(parsed["code"], "ABC")
                self.assertEqual(parsed["state"], "XYZ")
                self.assertEqual(parsed["host"], "oauth.example.com")
                self.assertEqual(parsed["extra_params"], {})

    def test_parse_redirect_url_accepts_business_auth_code_and_extra_params(self) -> None:
        parsed = s1_redirect.parse_redirect_url(
            "https://oauth.example.com/path?auth_code=AUTH123&state=STATE456&foo=bar&foo=baz"
        )

        self.assertEqual(parsed["code"], "AUTH123")
        self.assertEqual(parsed["state"], "STATE456")
        self.assertEqual(parsed["extra_params"], {"foo": ["bar", "baz"]})

    def test_parse_redirect_url_rejects_missing_code_or_state(self) -> None:
        with self.assertRaises(ValueError):
            _ = s1_redirect.parse_redirect_url("https://oauth.example.com/?state=XYZ")
        with self.assertRaises(ValueError):
            _ = s1_redirect.parse_redirect_url("https://oauth.example.com/?code=ABC")


class AuthUrlTests(unittest.TestCase):
    def test_display_auth_url_uses_pkce_s256_and_required_params(self) -> None:
        verifier = "verifier-for-test"
        with patch(
            "spikes.s1_redirect.secrets.token_urlsafe",
            side_effect=["STATE", verifier],
        ) as token:
            auth_url, state, pkce_verifier = s1_redirect.build_auth_url(
                "display",
                "app-id",
                "https://oauth.example.com",
                ["user.info.basic", "video.upload"],
            )

        parsed_url = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        verifier_digest = hashlib.sha256(verifier.encode()).digest()
        expected_challenge = base64.urlsafe_b64encode(verifier_digest).rstrip(b"=").decode()

        self.assertEqual(token.call_args_list[0].args, (32,))
        self.assertEqual(token.call_args_list[1].args, (64,))
        self.assertEqual(state, "STATE")
        self.assertEqual(pkce_verifier, verifier)
        self.assertEqual(
            parsed_url.geturl().split("?")[0],
            s1_redirect.DISPLAY_AUTH_URL.rstrip("?"),
        )
        self.assertEqual(params["client_key"], ["app-id"])
        self.assertEqual(params["scope"], ["user.info.basic,video.upload"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["redirect_uri"], ["https://oauth.example.com"])
        self.assertEqual(params["state"], ["STATE"])
        self.assertEqual(params["code_challenge"], [expected_challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])

    def test_business_auth_url_uses_app_id_state_redirect_only(self) -> None:
        with patch("spikes.s1_redirect.secrets.token_urlsafe", return_value="STATE"):
            auth_url, state, pkce_verifier = s1_redirect.build_auth_url(
                "business",
                "business-app-id",
                "https://oauth.example.com/business",
                ["ignored.scope"],
            )

        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        self.assertEqual(state, "STATE")
        self.assertIsNone(pkce_verifier)
        self.assertEqual(params, {
            "app_id": ["business-app-id"],
            "state": ["STATE"],
            "redirect_uri": ["https://oauth.example.com/business"],
        })


class TokenExchangeTests(unittest.TestCase):
    def test_display_exchange_posts_form_encoded_body(self) -> None:
        captured_requests: list[tuple[urllib.request.Request, int]] = []

        def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
            captured_requests.append((request, timeout))
            return FakeResponse({"access_token": "TOKEN"})

        with patch("spikes.s1_redirect.urllib.request.urlopen", side_effect=fake_urlopen):
            result = s1_redirect.exchange_code_display(
                "app-id",
                "secret",
                "CODE",
                "https://oauth.example.com",
                "VERIFIER",
            )

        request, timeout = captured_requests[0]
        request_data = cast(bytes, request.data)
        body: dict[str, list[str]] = urllib.parse.parse_qs(request_data.decode())
        self.assertEqual(timeout, 30)
        self.assertEqual(request.full_url, s1_redirect.DISPLAY_TOKEN_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(body["client_key"], ["app-id"])
        self.assertEqual(body["client_secret"], ["secret"])
        self.assertEqual(body["code"], ["CODE"])
        self.assertEqual(body["grant_type"], ["authorization_code"])
        self.assertEqual(body["redirect_uri"], ["https://oauth.example.com"])
        self.assertEqual(body["code_verifier"], ["VERIFIER"])
        self.assertEqual(result["access_token"], "TOKEN")

    def test_business_exchange_posts_json_body(self) -> None:
        captured_requests: list[tuple[urllib.request.Request, int]] = []

        def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
            captured_requests.append((request, timeout))
            return FakeResponse({"data": {"access_token": "TOKEN"}})

        with patch("spikes.s1_redirect.urllib.request.urlopen", side_effect=fake_urlopen):
            result = s1_redirect.exchange_code_business("app-id", "secret", "AUTHCODE")

        request, timeout = captured_requests[0]
        request_data = cast(bytes, request.data)
        body = cast(dict[str, str], json.loads(request_data.decode()))
        self.assertEqual(timeout, 30)
        self.assertEqual(request.full_url, s1_redirect.BUSINESS_TOKEN_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(body, {"app_id": "app-id", "secret": "secret", "auth_code": "AUTHCODE"})
        result_data = cast(dict[str, s1_redirect.JsonValue], result["data"])
        self.assertEqual(result_data["access_token"], "TOKEN")


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_print_payloads_recursively(self) -> None:
        redacted = cast(
            dict[str, s1_redirect.JsonValue],
            s1_redirect.redact_json_for_print(
                {
                    "access_token": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "data": {"refresh_token": "1234567890"},
                    "scope": "user.info.basic",
                }
            ),
        )
        redacted_data = cast(dict[str, s1_redirect.JsonValue], redacted["data"])

        self.assertEqual(redacted["access_token"], "ABCD…(len=26)")
        self.assertEqual(redacted_data["refresh_token"], "1234…(len=10)")
        self.assertEqual(redacted["scope"], "user.info.basic")


if __name__ == "__main__":
    _ = unittest.main()
