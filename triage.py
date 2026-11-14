"""Triage observability issues and optionally create backlog items."""
import argparse
import hashlib
import logging
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common import ApiError, TriageError, load_settings
from connectors.azure_boards import AzureBoards
from connectors.openrouter import describe
from connectors.sentry import Sentry
from connectors.slack import post_slack, slack_section, slack_webhook

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("triage")



class Ledger:
    def __init__(self, connection, initialize=True):
        self.connection = connection
        if initialize:
            connection.execute("""CREATE TABLE IF NOT EXISTS work_items (
                key TEXT PRIMARY KEY,
                work_item_id INTEGER
            )""")

    def recorded(self, key):
        row = self.connection.execute("SELECT work_item_id FROM work_items WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def claim(self, key):
        with self.connection:
            return self.connection.execute("INSERT OR IGNORE INTO work_items(key) VALUES (?)", (key,)).rowcount == 1

    def record(self, key, item_id):
        with self.connection:
            self.connection.execute("""INSERT INTO work_items VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET work_item_id = excluded.work_item_id
                WHERE work_items.work_item_id IS NULL""", (key, item_id))

    def clear_rejected_claim(self, key):
        with self.connection:
            self.connection.execute("DELETE FROM work_items WHERE key = ? AND work_item_id IS NULL", (key,))


def item_key(scope, source_identity):
    return "triage-" + hashlib.sha256(f"{scope}\n{source_identity}".encode()).hexdigest()


def handle_issue(source, tracker, ledger, assess, args, issue_id, evidence):
    identity = source.identity(args.environment, issue_id)
    key = item_key(tracker.scope, identity)
    reference = f"Triage reference: [{identity}]"
    if (recorded := ledger.recorded(key)) is not None:
        return "previously tracked", recorded, None
    if (existing := tracker.find(reference)) is not None:
        if args.apply:
            ledger.record(key, existing)
        return "already tracked", existing, None

    assessment = assess(issue_id, evidence)
    if not args.apply:
        LOG.info("Would create for issue %s: %s (suggested priority %s)", issue_id, assessment.title, assessment.priority)
        return "would create", None, assessment
    if not ledger.claim(key):
        return "needs reconciliation", None, None
    # Once claimed, another local run cannot submit a create for this issue.
    try:
        existing = tracker.find(reference)
    except (TriageError, KeyError, AttributeError, TypeError):
        ledger.clear_rejected_claim(key)
        raise
    if existing is not None:
        ledger.record(key, existing)
        return "already tracked", existing, None
    try:
        created = tracker.create(reference, args.environment, assessment, evidence,
                                 source.issue_link(issue_id, args.environment))
    except ApiError as error:
        # These statuses indicate a rejected request. Preserve the claim for an uncertain result.
        if error.status in (400, 401, 403, 404, 422, 429):
            ledger.clear_rejected_claim(key)
        raise
    ledger.record(key, created)
    return "created", created, assessment


def run(args, settings):
    source = Sentry(settings)
    tracker = AzureBoards(settings, args.apply) if args.apply or settings.get("AZDO_PAT") else None
    if tracker is None and (settings.get("AZDO_ORG") or settings.get("AZDO_PROJECT")):
        LOG.info("Azure Boards lookup is disabled until AZDO_PAT is configured.")

    def assess(issue_id, evidence):
        return describe(settings, issue_id, evidence)

    webhook = slack_webhook(settings) if args.slack else None
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    counts = Counter()
    slack_sections = []
    limited = False
    database = ROOT / "triage.sqlite3"
    read_only = not args.apply and database.is_file()
    storage_path = database.as_uri() + "?mode=ro" if read_only else database if args.apply else ":memory:"
    with closing(sqlite3.connect(storage_path, uri=read_only, timeout=15)) as conn:
        ledger = Ledger(conn, initialize=not read_only)
        try:
            issues, limited = source.candidates(args.environment, start, end, args.limit)
        except TriageError as error:
            LOG.error("%s", error)
            counts["failed"] += 1
            issues = []
        for issue_id in issues:
            try:
                evidence = source.evidence(issue_id, args.environment, start, end)
                if evidence is None:
                    counts["no matching event"] += 1
                    continue
                if tracker:
                    outcome, item_id, wording = handle_issue(source, tracker, ledger, assess, args, issue_id, evidence)
                else:
                    outcome, item_id, wording = "candidate", None, assess(issue_id, evidence)
                    LOG.info("Issue %s: %s (suggested priority %s)", issue_id, wording.title, wording.priority)
                counts[outcome] += 1
                if item_id:
                    LOG.info("Issue %s: %s (%s)", issue_id, outcome, tracker.item_link(item_id))
                elif outcome == "needs reconciliation":
                    LOG.warning("Issue %s needs reconciliation; local key %s", issue_id,
                                item_key(tracker.scope, source.identity(args.environment, issue_id)))
                if webhook and (wording or item_id):
                    source_url = source.issue_link(issue_id, args.environment)
                    work_item_url = tracker.item_link(item_id) if item_id else None
                    if wording:
                        title = wording.title
                        detail = f"Suggested priority: {wording.priority}/4. {wording.summary}"
                    else:
                        title, detail = f"Work item #{item_id}", outcome.capitalize()
                    if item_id and wording:
                        title = f"[Triage/{args.environment}] {title}"
                    slack_sections.append(slack_section(title, detail, source_url, work_item_url))
            except (TriageError, KeyError, AttributeError, TypeError) as error:
                LOG.error("Issue %s: %s", issue_id,
                          error if isinstance(error, TriageError) else "unexpected API response")
                counts["failed"] += 1
    mode = "run" if args.apply else "preview"
    outcomes = ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items())) or "no candidates"
    summary = f"Triage {mode} [{args.environment}, last {args.days} days]: {outcomes}."
    if limited:
        summary += f" Candidate limit reached ({args.limit})."
    LOG.info("%s", summary)
    slack_failed = False
    if webhook:
        try:
            post_slack(webhook, summary, slack_sections)
        except TriageError as error:
            LOG.error("%s", error)
            slack_failed = True
    return int(bool(counts["failed"] or counts["needs reconciliation"] or slack_failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, help="Exact source environment label, such as production")
    parser.add_argument("--days", type=int, choices=range(1, 31), default=7, metavar="1-30")
    parser.add_argument("--limit", type=int, choices=range(1, 100), default=20, metavar="1-99")
    parser.add_argument("--apply", action="store_true", help="Create Azure DevOps backlog items")
    parser.add_argument("--slack", action="store_true", help="Post the triage summary to Slack")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.environment):
        parser.error("--environment must be a single environment label")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return run(args, load_settings(ROOT / ".env.local"))
    except (OSError, UnicodeError, sqlite3.Error, TriageError) as error:
        LOG.error("%s", error if isinstance(error, TriageError) else "Could not read configuration or update local state")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
