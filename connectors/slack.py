"""Post readable triage reports with a Slack incoming webhook."""
import html
import re
from urllib.parse import urlsplit

from common import TriageError, request, required



def slack_webhook(settings):
    url = required(settings, "SLACK_WEBHOOK_URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.netloc in ("hooks.slack.com", "hooks.slack-gov.com")
                and re.fullmatch(r"/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", parts.path)
                and not parts.query and not parts.fragment):
            return url
    except ValueError:
        pass
    raise TriageError("SLACK_WEBHOOK_URL must be a Slack incoming webhook URL.")


def slack_section(title, summary, source_url, work_item_url=None):
    title = html.escape(title.replace("|", "｜"), quote=False)
    text = f"*<{work_item_url or source_url}|{title}>*\n{html.escape(summary, quote=False)}"
    links = f"<{source_url}|Open source issue>"
    if work_item_url:
        links = f"<{work_item_url}|Open work item> · {links}"
    text += f"\n{links}"
    return {"type": "section", "expand": False,
            "text": {"type": "mrkdwn", "verbatim": True, "text": text}}


def post_slack(webhook, summary, sections):
    batch_size = 10
    for start in range(0, max(len(sections), 1), batch_size):
        heading = summary if start == 0 else "Triage continued"
        batch = sections[start:start + batch_size]
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": heading}}, *batch]
        fallback = heading
        for section in batch:
            title, _, links = section["text"]["text"].split("\n", 2)
            fallback += f"\n{title}\n{links}"
        response = request("Slack summary", webhook, payload={"text": fallback, "blocks": blocks,
                           "unfurl_links": False, "unfurl_media": False}, expect_json=False)
        if response.strip() != b"ok":
            raise TriageError("Slack did not confirm delivery.")
