"""Configuration and HTTP helpers shared by the CLI and connectors."""
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener



class TriageError(Exception):
    pass


@dataclass(frozen=True)
class Assessment:
    title: str
    summary: str
    priority: int


class ApiError(TriageError):
    def __init__(self, service, status=None):
        self.status = status
        super().__init__(f"{service}: HTTP {status}" if status else f"{service}: request failed")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


def request(service, url, *, headers=None, payload=None, content_type="application/json", expect_json=True):
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        request_headers["Content-Type"] = content_type
        data = json.dumps(payload).encode("utf-8")
    try:
        with OPENER.open(Request(url, data=data, headers=request_headers), timeout=25) as response:
            body = response.read(8 * 1024 * 1024 + 1)
    except HTTPError as error:
        error.close()
        raise ApiError(service, error.code) from None
    except (URLError, OSError, ValueError):
        raise ApiError(service) from None
    if len(body) > 8 * 1024 * 1024:
        raise TriageError(f"{service}: response exceeded size limit")
    if not expect_json:
        return body
    try:
        return json.loads(body)
    except (UnicodeError, ValueError):
        raise TriageError(f"{service}: invalid JSON response") from None


def load_settings(path):
    if not path.is_file():
        raise TriageError("Create .env.local from .env.local.example before running triage.")
    settings = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in settings:
            raise TriageError(f"Invalid or duplicate setting on .env.local line {number}.")
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if not all(c.isprintable() for c in value):
            raise TriageError(f"Invalid characters on .env.local line {number}.")
        settings[key] = value
    return settings


def required(settings, key, pattern=None):
    value = settings.get(key)
    if not value or (pattern and not re.fullmatch(pattern, value)):
        raise TriageError(f"Missing or invalid setting {key}.")
    return value
