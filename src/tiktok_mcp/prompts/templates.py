"""FastMCP Prompt templates for tiktok-mcp.

Three user-facing templates wire a parameterized brief into the
assistant's context. Each template:

- accepts only typed parameters (no hard-coded aliases, dates, or IDs)
- quotes the exact MCP tool names the assistant should call
- ends with a one-line "Safety reminder:" footer naming the relevant
  write gate or stating that the flow is read-only

Importing this module registers the prompts on the shared FastMCP
``app`` instance via the ``@app.prompt`` decorator.
"""

from __future__ import annotations

from mcp.server.fastmcp.prompts.base import Message, UserMessage

from tiktok_mcp.server import app

WEEKLY_MARKETING_REPORT_NAME = "weekly_marketing_report"
COMMENT_QUEUE_TRIAGE_NAME = "comment_queue_triage"
WEEKLY_ENGAGEMENT_SUMMARY_NAME = "weekly_engagement_summary"


@app.prompt(
    name=WEEKLY_MARKETING_REPORT_NAME,
    description=(
        "Run a weekly TikTok Marketing BASIC report for one advertiser "
        "alias across a date range and summarize the top spend, CPM, "
        "CTR, and CPC metrics in both English and Norwegian, always "
        "annotated with the report's currency_code."
    ),
)
def weekly_marketing_report(
    advertiser_alias: str,
    start_date: str,
    end_date: str,
) -> list[Message]:
    template = f"""You are running a weekly TikTok Marketing performance report.

Inputs for this run:
- advertiser_alias: {advertiser_alias}
- start_date: {start_date}
- end_date: {end_date}

Step 1. Call `marketing_run_async_report` with:
- alias = "{advertiser_alias}"
- report_type = "BASIC"
- data_level = "AUCTION_ADVERTISER"
- start_date = "{start_date}"
- end_date = "{end_date}"
- a reasonable set of dimensions (e.g. ["advertiser_id"]) and metrics
  that cover spend, impressions, clicks, CPM, CTR, and CPC

The asynchronous flow requires an advertiser_id alongside the alias.
If you do not already know it, call `marketing_list_bc_advertisers`
first and confirm the right advertiser with the human before continuing.

Step 2. Poll the task with `marketing_poll_async_report` (same alias,
same advertiser_id, the task_id returned by step 1) until the status
field reads SUCCESS (or the equivalent completed state).

Step 3. Download the rows with `marketing_download_async_report` using
the same alias, advertiser_id, and task_id. The response includes a
top-level `currency_code` plus a per-row `currency_code`.

Step 4. Summarize the top spend, CPM, CTR, and CPC rows in English.
Then re-state the same summary briefly in Norwegian. Oppsummer de
viktigste tallene på norsk i tillegg til engelsk.

Always annotate every monetary metric with the report's currency_code
(for example "1234 NOK", "987 SEK", "456 DKK", "321 EUR"). Never mix
currencies in a single total: TikTok reports can return rows in
different currencies depending on the advertiser, and v0.1 does not
cross-convert.

Safety reminder: this is a read-only flow. No writes required."""
    return [UserMessage(template)]


@app.prompt(
    name=COMMENT_QUEUE_TRIAGE_NAME,
    description=(
        "Pull a batch of comments for one TikTok video, classify each "
        "into SPAM, QUESTION, COMPLIMENT, NEGATIVE_FEEDBACK, or "
        "OFF_TOPIC, and propose draft replies for QUESTION and "
        "COMPLIMENT items. The prompt stops before any write and "
        "requires explicit human confirmation before posting."
    ),
)
def comment_queue_triage(
    account_alias: str,
    video_id: str,
    max_comments: int = 50,
) -> list[Message]:
    template = f"""You are triaging the latest TikTok comment queue on one video.

Inputs for this run:
- account_alias: {account_alias}
- video_id: {video_id}
- max_comments: {max_comments}

Step 1. Call `comments_list` with:
- alias = "{account_alias}"
- post_id = "{video_id}"
- page = 1
- page_size = up to {max_comments} (cap at 30 per API call; paginate
  if more are requested)
- sort_by = "newest"

The corresponding `advertiser_id` is required by the tool; ask the
human for it if you do not already have it cached for this alias.

Step 2. For each comment, classify the comment_id into exactly one of:
- SPAM
- QUESTION
- COMPLIMENT
- NEGATIVE_FEEDBACK
- OFF_TOPIC

Step 3. For each QUESTION and each COMPLIMENT, propose a short draft
reply text (one to two sentences, friendly tone, no emojis unless the
original comment used them).

Step 4. Report back ONLY:
- the count per classification bucket
- the list of `comment_id` values per bucket
- the proposed draft reply text for each QUESTION and COMPLIMENT

Do NOT include the full raw comment_text in your final report. Comment
bodies are sensitive user content and should stay out of summaries.

Step 5. STOP. Ask the human to confirm each draft reply before any
write happens. Do not call `post_comment_reply`, `hide_comment`,
`pin_top_comment`, `unpin_top_comment`, `unhide_comment`, or
`delete_own_reply` until the human explicitly approves each action.

Safety reminder: posting replies or hiding comments requires
`TIKTOK_MCP_ALLOW_WRITES=comments` (or `=all`). Ask the human before
each write action."""
    return [UserMessage(template)]


@app.prompt(
    name=WEEKLY_ENGAGEMENT_SUMMARY_NAME,
    description=(
        "Pull recent Display API videos for one account, aggregate "
        "view, like, comment, and share totals, and identify the "
        "top three videos by engagement-rate "
        "(likes + comments + shares) / views."
    ),
)
def weekly_engagement_summary(
    display_alias: str,
    days: int = 7,
) -> list[Message]:
    template = f"""You are producing a weekly engagement summary for one TikTok
Display API account.

Inputs for this run:
- display_alias: {display_alias}
- days: {days}

Step 1. Call `display_list_videos` with:
- alias = "{display_alias}"
- cursor = None on the first call
- max_count = 20 per page (paginate by passing the returned cursor
  back in on follow-up calls until has_more is false OR the videos
  fall outside the last {days} days)

Step 2. Filter the collected videos to those whose `create_time`
falls within the last {days} days from now.

Step 3. Aggregate totals across the filtered set:
- total view_count
- total like_count
- total comment_count
- total share_count

Step 4. For each video, compute the engagement rate:
engagement_rate = (like_count + comment_count + share_count) / view_count

Treat view_count == 0 as engagement_rate = 0 to avoid divide-by-zero.

Step 5. Identify the top 3 videos by engagement_rate. For each, report
the video id, the engagement_rate (rounded to 4 decimal places), and
the raw like/comment/share/view counts.

Safety reminder: this is a read-only summary. No write env vars needed."""
    return [UserMessage(template)]


__all__ = [
    "COMMENT_QUEUE_TRIAGE_NAME",
    "WEEKLY_ENGAGEMENT_SUMMARY_NAME",
    "WEEKLY_MARKETING_REPORT_NAME",
    "comment_queue_triage",
    "weekly_engagement_summary",
    "weekly_marketing_report",
]
