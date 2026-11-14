"""Read unresolved Sentry issues and reduce events to selected evidence."""
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from common import ApiError, TriageError, request, required

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,79}")
SOURCE_FILE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,69}\.(?:cs|js|jsx|ts|tsx|vue|py|java|go|swift)")
SENSITIVE = re.compile(r"password|secret|token|api_?key|[a-f0-9]{24,}|[A-Za-z0-9_+/=-]{36,}", re.I)


def safe_symbol(value, pattern):
    if isinstance(value, str) and pattern.fullmatch(value) and not SENSITIVE.search(value):
        return value
    return None


def extract_evidence(event):
    for entry in event.get("entries") or []:
        if entry.get("type") != "exception":
            continue
        values = (entry.get("data") or {}).get("values") or []
        for value in reversed(values):
            kind = safe_symbol(value.get("type"), IDENTIFIER) or "Error"
            status = re.search(r"(?:status code|HTTP)\s+([45][0-9]{2})\b", value.get("value") or "", re.I)
            frames = []
            for frame in reversed((value.get("stacktrace") or {}).get("frames") or []):
                if frame.get("inApp") is not True:
                    continue
                filename = re.split(r"[/\\]", frame.get("filename") or "")[-1]
                file = safe_symbol(filename, SOURCE_FILE)
                function = safe_symbol(frame.get("function"), IDENTIFIER)
                line = frame.get("lineNo")
                if file or function:
                    frames.append({"file": file, "function": function,
                                   "line": line if type(line) is int and 0 < line < 10_000_000 else None})
                if len(frames) == 6:
                    break
            return {"type": kind, "http_status": int(status[1]) if status else None, "frames": frames}
    return {"type": "Error", "http_status": None, "frames": []}


class Sentry:
    def __init__(self, settings):
        region = settings.get("SENTRY_REGION", "us")
        if region not in ("us", "de"):
            raise TriageError("SENTRY_REGION must be us or de.")
        self.origin = "https://sentry.io" if region == "us" else "https://de.sentry.io"
        self.org = required(settings, "SENTRY_ORG", r"[A-Za-z0-9_-]+")
        self.project = required(settings, "SENTRY_PROJECT", r"[A-Za-z0-9_-]+")
        self.headers = {"Authorization": "Bearer " + required(settings, "SENTRY_READ_TOKEN")}
        self.issues_url = f"{self.origin}/api/0/organizations/{self.org}/issues/"

    def identity(self, environment, issue_id):
        return f"sentry/{self.org.lower()}/{environment}/{issue_id}"

    def candidates(self, environment, start, end, limit):
        filters = {"environment": environment, "start": start.isoformat(), "end": end.isoformat()}
        query = {**filters, "project": self.project, "query": "is:unresolved issue.category:error",
                 "sort": "date", "limit": limit + 1}
        issues = request("Sentry issues", self.issues_url + "?" + urlencode(query), headers=self.headers)
        if not isinstance(issues, list):
            raise TriageError("Sentry issues: expected an issue list.")
        identifiers = []
        for issue in issues[:limit]:
            issue_id = str(issue.get("id", "")) if isinstance(issue, dict) else ""
            if not re.fullmatch(r"[0-9]{1,20}", issue_id):
                raise TriageError("Sentry returned an invalid issue ID.")
            identifiers.append(issue_id)
        return list(dict.fromkeys(identifiers)), len(issues) > limit

    def evidence(self, issue_id, environment, start, end):
        filters = {"environment": environment, "start": start.isoformat(), "end": end.isoformat()}
        url = f"{self.issues_url}{issue_id}/events/latest/?{urlencode(filters)}"
        try:
            event = request("Sentry event", url, headers=self.headers)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        labels = [tag.get("value") for tag in event.get("tags") or [] if tag.get("key") == "environment"]
        try:
            occurred_at = datetime.fromisoformat(event.get("dateCreated") or "")
        except (TypeError, ValueError):
            return None
        if labels != [environment] or occurred_at.tzinfo is None or not start <= occurred_at <= end:
            return None
        return {**extract_evidence(event), "occurred_at": occurred_at.astimezone(UTC).isoformat()}

    def issue_link(self, issue_id, environment):
        return f"{self.origin}/organizations/{self.org}/issues/{issue_id}/?environment={quote(environment, safe='')}"
