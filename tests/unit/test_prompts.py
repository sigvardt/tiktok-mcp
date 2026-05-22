from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.prompts.base import Prompt, PromptArgument

from tiktok_mcp.prompts.templates import (
    COMMENT_QUEUE_TRIAGE_NAME,
    WEEKLY_ENGAGEMENT_SUMMARY_NAME,
    WEEKLY_MARKETING_REPORT_NAME,
)
from tiktok_mcp.server import app

EXPECTED_PROMPT_NAMES = {
    WEEKLY_MARKETING_REPORT_NAME,
    COMMENT_QUEUE_TRIAGE_NAME,
    WEEKLY_ENGAGEMENT_SUMMARY_NAME,
}


@pytest.fixture(scope="module")
def registered_prompts() -> dict[str, Prompt]:
    prompts = asyncio.run(app.list_prompts())
    return {p.name: p for p in prompts if p.name in EXPECTED_PROMPT_NAMES}


def _arg_names(prompt: Prompt) -> set[str]:
    assert prompt.arguments is not None
    return {arg.name for arg in prompt.arguments}


def _required_arg_names(prompt: Prompt) -> set[str]:
    assert prompt.arguments is not None
    return {arg.name for arg in prompt.arguments if arg.required}


def _arg(prompt: Prompt, name: str) -> PromptArgument:
    assert prompt.arguments is not None
    matches = [arg for arg in prompt.arguments if arg.name == name]
    assert matches, f"argument {name!r} not registered on {prompt.name!r}"
    return matches[0]


def _render(name: str, arguments: dict[str, object]) -> str:
    result = asyncio.run(app.get_prompt(name, arguments))
    assert result.messages, f"prompt {name!r} rendered no messages"
    rendered_text_chunks: list[str] = []
    for message in result.messages:
        assert message.role == "user", f"{name} produced role {message.role!r}"
        content = message.content
        assert getattr(content, "type", None) == "text"
        rendered_text_chunks.append(getattr(content, "text", ""))
    return "\n".join(rendered_text_chunks)


def test_all_three_prompts_register(registered_prompts: dict[str, Prompt]) -> None:
    assert set(registered_prompts) == EXPECTED_PROMPT_NAMES


def test_weekly_marketing_report_argument_shape(
    registered_prompts: dict[str, Prompt],
) -> None:
    prompt = registered_prompts[WEEKLY_MARKETING_REPORT_NAME]
    assert _arg_names(prompt) == {"advertiser_alias", "start_date", "end_date"}
    assert _required_arg_names(prompt) == {
        "advertiser_alias",
        "start_date",
        "end_date",
    }


def test_comment_queue_triage_argument_shape(
    registered_prompts: dict[str, Prompt],
) -> None:
    prompt = registered_prompts[COMMENT_QUEUE_TRIAGE_NAME]
    assert _arg_names(prompt) == {"account_alias", "video_id", "max_comments"}
    assert _required_arg_names(prompt) == {"account_alias", "video_id"}
    assert not _arg(prompt, "max_comments").required


def test_weekly_engagement_summary_argument_shape(
    registered_prompts: dict[str, Prompt],
) -> None:
    prompt = registered_prompts[WEEKLY_ENGAGEMENT_SUMMARY_NAME]
    assert _arg_names(prompt) == {"display_alias", "days"}
    assert _required_arg_names(prompt) == {"display_alias"}
    assert not _arg(prompt, "days").required


def test_each_prompt_has_user_facing_description(
    registered_prompts: dict[str, Prompt],
) -> None:
    for name in EXPECTED_PROMPT_NAMES:
        description = registered_prompts[name].description
        assert description, f"{name} missing description"
        assert len(description) >= 40, f"{name} description too short: {description!r}"


def test_weekly_marketing_report_text_includes_currency_and_dates() -> None:
    text = _render(
        WEEKLY_MARKETING_REPORT_NAME,
        {
            "advertiser_alias": "no-marketing-001",
            "start_date": "2026-05-15",
            "end_date": "2026-05-21",
        },
    )
    assert "currency" in text.lower()
    assert "2026-05-15" in text
    assert "2026-05-21" in text
    assert "no-marketing-001" in text
    assert "marketing_run_async_report" in text
    assert "marketing_poll_async_report" in text
    assert "marketing_download_async_report" in text
    assert "BASIC" in text
    assert "norsk" in text.lower() or "norwegian" in text.lower()
    assert "Safety reminder:" in text
    assert "read-only" in text.lower()


def test_comment_queue_triage_text_surfaces_writes_gate_and_classes() -> None:
    text = _render(
        COMMENT_QUEUE_TRIAGE_NAME,
        {
            "account_alias": "no-business-001",
            "video_id": "7398765432109876543",
            "max_comments": 25,
        },
    )
    assert "TIKTOK_MCP_ALLOW_WRITES" in text
    assert "comments_list" in text
    assert "no-business-001" in text
    assert "7398765432109876543" in text
    for classification in (
        "SPAM",
        "QUESTION",
        "COMPLIMENT",
        "NEGATIVE_FEEDBACK",
        "OFF_TOPIC",
    ):
        assert classification in text, f"missing classification {classification}"
    assert "Safety reminder:" in text
    assert "confirm" in text.lower() or "confirmation" in text.lower()


def test_comment_queue_triage_does_not_auto_post() -> None:
    text = _render(
        COMMENT_QUEUE_TRIAGE_NAME,
        {
            "account_alias": "no-business-001",
            "video_id": "7398765432109876543",
        },
    )
    lowered = text.lower()
    forbidden_phrases = (
        "automatically post",
        "auto-post",
        "auto post",
        "without confirmation",
        "without asking",
    )
    for phrase in forbidden_phrases:
        assert phrase not in lowered, f"prompt allows {phrase!r}"


def test_weekly_engagement_summary_text_is_read_only() -> None:
    text = _render(
        WEEKLY_ENGAGEMENT_SUMMARY_NAME,
        {"display_alias": "no-display-001", "days": 14},
    )
    assert "display_list_videos" in text
    assert "no-display-001" in text
    assert "14" in text
    assert "engagement" in text.lower()
    assert "TIKTOK_MCP_ALLOW_WRITES" not in text
    assert "Safety reminder:" in text
    assert "read-only" in text.lower()


def test_no_prompt_embeds_hardcoded_credentials() -> None:
    forbidden_substrings = (
        "client_secret=",
        "access_token=",
        "refresh_token=",
        "Authorization: Bearer ",
    )
    samples: list[str] = [
        _render(
            WEEKLY_MARKETING_REPORT_NAME,
            {
                "advertiser_alias": "alias-a",
                "start_date": "2026-05-01",
                "end_date": "2026-05-07",
            },
        ),
        _render(
            COMMENT_QUEUE_TRIAGE_NAME,
            {"account_alias": "alias-b", "video_id": "1234"},
        ),
        _render(
            WEEKLY_ENGAGEMENT_SUMMARY_NAME,
            {"display_alias": "alias-c"},
        ),
    ]
    for text in samples:
        for forbidden in forbidden_substrings:
            assert forbidden not in text, f"prompt leaks {forbidden!r}"


def test_render_parameters_are_substituted_not_hardcoded() -> None:
    text_a = _render(
        WEEKLY_MARKETING_REPORT_NAME,
        {
            "advertiser_alias": "alias-AAA",
            "start_date": "2026-01-01",
            "end_date": "2026-01-07",
        },
    )
    text_b = _render(
        WEEKLY_MARKETING_REPORT_NAME,
        {
            "advertiser_alias": "alias-BBB",
            "start_date": "2026-02-01",
            "end_date": "2026-02-07",
        },
    )
    assert "alias-AAA" in text_a
    assert "alias-AAA" not in text_b
    assert "alias-BBB" in text_b
    assert "2026-01-07" in text_a
    assert "2026-02-07" in text_b
