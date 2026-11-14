import argparse
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import triage
from common import ApiError
from connectors import azure_boards, openrouter, sentry, slack


class TriageTests(unittest.TestCase):
    settings = {
        "SENTRY_ORG": "example", "SENTRY_PROJECT": "sample", "SENTRY_READ_TOKEN": "fake",
        "AZDO_ORG": "example", "AZDO_PROJECT": "Sample", "AZDO_PAT": "fake",
        "AZDO_AREA_PATH": "Sample", "AZDO_BACKLOG_ITERATION_PATH": "Sample",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/EXAMPLE/EXAMPLE/fake",
    }

    def setUp(self):
        self.now = datetime.now(UTC) - timedelta(minutes=1)
        self.event = {
            "dateCreated": self.now.isoformat(), "tags": [{"key": "environment", "value": "dev"}],
            "user": {"email": "hidden@example.test"},
            "entries": [{"type": "exception", "data": {"values": [{
                "type": "TypeError", "value": "HTTP 500 secret=hidden",
                "stacktrace": {"frames": [{"inApp": True, "filename": "/private/example.py",
                                           "function": "process", "vars": {"secret": "hidden"}}]},
            }]}}],
        }

    def test_sentry_selects_exact_environment_and_minimizes(self):
        source = sentry.Sentry(self.settings)
        start, end = self.now - timedelta(days=7), self.now + timedelta(minutes=1)
        with patch.object(sentry, "request", return_value=self.event) as send:
            self.assertIsNone(source.evidence("123", "production", start, end))
            evidence = source.evidence("123", "dev", start, end)
        self.assertIn("environment=dev", send.call_args.args[1])
        self.assertEqual("example.py", evidence["frames"][0]["file"])
        self.assertNotIn("hidden", str(evidence))
        self.assertNotIn("/private", str(evidence))

    def test_ai_scope_and_priority_validation(self):
        evidence = sentry.extract_evidence(self.event)
        settings = {"OPENROUTER_API_KEY": "fake"}
        result = {"choices": [{"message": {"content":
                  '{"title":"Check processing","summary":"Verify the failure.","priority":2}'}}]}
        with patch.object(openrouter, "request", return_value=result) as send:
            self.assertEqual(2, openrouter.describe(settings, "123", evidence).priority)
            payload = send.call_args.kwargs["payload"]
            self.assertEqual({"type": "TypeError", "http_status": 500, "frames": []},
                             json.loads(payload["messages"][1]["content"]))
            openrouter.describe({**settings, "AI_INCLUDE_STACK": "true"}, "123", evidence)
            self.assertIn("example.py", send.call_args.kwargs["payload"]["messages"][1]["content"])
        with patch.object(openrouter, "request", return_value={"choices": []}):
            with self.assertLogs("triage", level="WARNING"):
                self.assertEqual(3, openrouter.describe(settings, "123", evidence).priority)

    def test_apply_and_slack_are_independent_and_deduplicate(self):
        args = argparse.Namespace(environment="dev", days=7, limit=20, apply=False, slack=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(triage, "ROOT", Path(directory)), \
                patch.object(sentry, "request", side_effect=lambda service, *_a, **_k:
                             [{"id": "123"}] if service == "Sentry issues" else self.event), \
                patch.object(azure_boards, "request", side_effect=lambda service, *_a, **_k:
                             {"workItems": []} if service == "Azure Boards lookup" else {"id": 42}) as boards, \
                patch.object(slack, "request", return_value=b"ok") as messenger:
            source_only = {k: v for k, v in self.settings.items() if not k.startswith("AZDO_")}
            self.assertEqual(0, triage.run(args, source_only))
            boards.assert_not_called()
            self.assertFalse((Path(directory) / "triage.sqlite3").exists())
            self.assertIn("Open source issue", messenger.call_args.kwargs["payload"]["text"])
            args.apply, args.slack = True, False
            self.assertEqual(0, triage.run(args, self.settings))
            self.assertEqual(1, messenger.call_count)
            patch_operations = boards.call_args.kwargs["payload"]
            fields = {operation["path"]: operation["value"] for operation in patch_operations}
            self.assertEqual("dev", fields["/fields/System.Tags"])
            self.assertIn("Suggested priority: 3/4", fields["/fields/Microsoft.VSTS.TCM.ReproSteps"])
            self.assertIn("Triage reference: [sentry/example/dev/123]", fields["/fields/System.Description"])
            args.slack = True
            self.assertEqual(0, triage.run(args, self.settings))
            self.assertEqual(3, boards.call_count)
            message = messenger.call_args.kwargs["payload"]["text"]
            self.assertIn("Open work item", message)
            self.assertIn("Open source issue", message)

    def test_uncertain_create_is_not_retried(self):
        args = argparse.Namespace(environment="dev", days=7, limit=20, apply=True, slack=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(triage, "ROOT", Path(directory)), \
                patch.object(sentry, "request", side_effect=lambda service, *_a, **_k:
                             [{"id": "123"}] if service == "Sentry issues" else self.event):
            def boards_request(service, *args, **kwargs):
                if service == "Azure Boards create":
                    raise ApiError(service, 503)
                return {"workItems": []}
            with patch.object(azure_boards, "request", side_effect=boards_request) as boards:
                with self.assertLogs("triage", level="WARNING"):
                    self.assertEqual(1, triage.run(args, self.settings))
                    self.assertEqual(1, triage.run(args, self.settings))
                self.assertEqual(1, sum(call.args[0] == "Azure Boards create" for call in boards.call_args_list))


if __name__ == "__main__":
    unittest.main()
