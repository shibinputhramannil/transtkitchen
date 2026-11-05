"""
Receives Content Security Policy violation reports from browsers and writes a
minimal, sanitized summary of each to the log. Nothing is stored.

Browsers send reports without signing in, in two formats:

- report-uri: Content-Type application/csp-report, {"csp-report": {...}}
- report-to (Reporting API): Content-Type application/reports+json, one report
  or a list, each {"type": "csp-violation", "body": {...}}

Reports are attacker-controlled input, so only these fields are kept, each
reduced before logging: the disposition, the directive, the blocked resource
(a keyword such as "inline", or just its origin), the page path with IDs
replaced and without its query string, the source file on the frontend's own
origin with line and column, the HTTP status, and the browser family and major
version. This module never logs client IP addresses, full user agents,
referrers, code samples, the policy text, query strings, or fragments (the
server's access log is separate and records every request). Reports for pages
outside the frontend origins (CORS_ALLOWED_ORIGINS) and reports caused by
browser extensions are ignored.
"""

import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
MAX_REPORTS_PER_REQUEST = 20
# Each distinct violation is logged at most once per window and process, and
# at most this many distinct violations per window, so a flood cannot fill the log.
WINDOW_SECONDS = 3600
MAX_KEYS_PER_WINDOW = 200

EXTENSION_SCHEMES = {
    "chrome-extension",
    "moz-extension",
    "safari-extension",
    "safari-web-extension",
    "ms-browser-extension",
}
BLOCKED_KEYWORDS = {"inline", "eval", "wasm-eval", "self", "trusted-types-policy", "trusted-types-sink"}
SPECIAL_SCHEMES = {"data", "blob", "filesystem", "mediastream"}
WEB_SCHEMES = {"http", "https", "ws", "wss"}

DIRECTIVE = re.compile(r"[a-z][a-z-]{0,39}")
HOSTNAME = re.compile(r"[a-z0-9.-]{1,253}")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NUMBER_SEGMENT = re.compile(r"(?<=/)\d+(?=/|$)")
UNSAFE_PATH_CHARACTERS = re.compile(r"[^A-Za-z0-9/._~:%-]")
MAX_PATH_LENGTH = 120
MAX_POSITION = 10_000_000

BROWSERS = [
    ("Edge", re.compile(r"\bEdg(?:e|A|iOS)?/(\d+)")),
    ("Opera", re.compile(r"\bOPR/(\d+)")),
    ("Samsung Internet", re.compile(r"\bSamsungBrowser/(\d+)")),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"\b(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"\bVersion/(\d+)[^ ]* (?:Mobile/\S+ )?Safari/")),
]

_lock = threading.Lock()
_window_started = None
_logged_keys = set()
_cap_reported = False


def _now():
    return time.monotonic()


def reset_state():
    """Forget which violations were logged (used by tests)."""
    global _window_started, _cap_reported
    with _lock:
        _window_started = None
        _logged_keys.clear()
        _cap_reported = False


def browser_from_user_agent(user_agent):
    for name, pattern in BROWSERS:
        match = pattern.search(user_agent or "")
        if match:
            return f"{name} {match.group(1)}"
    return "other"


def _origin(url):
    """scheme://host[:port] for a web URL, or None if it is not one."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except (AttributeError, ValueError):
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in WEB_SCHEMES or not HOSTNAME.fullmatch(host):
        return None
    default_port = {"http": 80, "ws": 80, "https": 443, "wss": 443}[scheme]
    return f"{scheme}://{host}" + (f":{port}" if port and port != default_port else "")


def _scheme(value):
    return value.split(":", 1)[0].strip().lower() if ":" in value else ""


def _clean_path(url):
    path = urlsplit(url).path or "/"
    path = UUID.sub(":id", path)
    path = NUMBER_SEGMENT.sub(":n", path)
    return UNSAFE_PATH_CHARACTERS.sub("_", path)[:MAX_PATH_LENGTH]


def _position(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 <= number <= MAX_POSITION else None


def _status(value):
    number = _position(value)
    return number if number is not None and number <= 999 else None


def extract_reports(payload):
    """The violation reports in a request body, in one common shape."""
    if isinstance(payload, dict) and "csp-report" in payload:
        report = payload["csp-report"]
        if not isinstance(report, dict):
            return []
        violated = report.get("violated-directive")
        return [
            {
                "document": report.get("document-uri"),
                "blocked": report.get("blocked-uri"),
                "directive": report.get("effective-directive")
                or (violated.split()[0] if isinstance(violated, str) and violated.split() else None),
                "disposition": report.get("disposition"),
                "status": report.get("status-code"),
                "source": report.get("source-file"),
                "line": report.get("line-number"),
                "column": report.get("column-number"),
            }
        ]
    items = payload if isinstance(payload, list) else [payload]
    reports = []
    for item in items[:MAX_REPORTS_PER_REQUEST]:
        if not isinstance(item, dict) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        reports.append(
            {
                "document": body.get("documentURL") or item.get("url"),
                "blocked": body.get("blockedURL"),
                "directive": body.get("effectiveDirective"),
                "disposition": body.get("disposition"),
                "status": body.get("statusCode"),
                "source": body.get("sourceFile"),
                "line": body.get("lineNumber"),
                "column": body.get("columnNumber"),
            }
        )
    return reports


def summarize(report, own_origins):
    """The loggable fields of one report, or None if it should be ignored."""
    document = report.get("document")
    if not isinstance(document, str) or _origin(document) not in own_origins:
        return None

    blocked = report.get("blocked")
    blocked = blocked.strip() if isinstance(blocked, str) else ""
    blocked_scheme = _scheme(blocked)
    if blocked_scheme in EXTENSION_SCHEMES:
        return None
    if not blocked:
        blocked = "none"
    elif blocked.lower() in BLOCKED_KEYWORDS:
        blocked = blocked.lower()
    elif blocked_scheme in SPECIAL_SCHEMES:
        blocked = blocked_scheme
    elif blocked_scheme in WEB_SCHEMES:
        blocked = _origin(blocked) or "invalid"
    else:
        blocked = "other"

    source = report.get("source")
    source = source.strip() if isinstance(source, str) else ""
    if _scheme(source) in EXTENSION_SCHEMES:
        return None
    source_origin = _origin(source) if source else None
    if source_origin is None:
        source = "-"
    elif source_origin in own_origins:
        source = source_origin + _clean_path(source)
        line, column = _position(report.get("line")), _position(report.get("column"))
        if line is not None:
            source += f":{line}" + (f":{column}" if column is not None else "")
    else:
        source = source_origin

    directive = report.get("directive")
    disposition = report.get("disposition")
    status_code = _status(report.get("status"))
    return {
        "disposition": disposition if disposition in ("enforce", "report") else "unknown",
        "directive": directive if isinstance(directive, str) and DIRECTIVE.fullmatch(directive) else "unknown",
        "blocked": blocked,
        "page": _clean_path(document),
        "source": source,
        "status": "-" if status_code is None else str(status_code),
    }


def _should_log(key, now):
    global _window_started, _cap_reported
    with _lock:
        if _window_started is None or now - _window_started >= WINDOW_SECONDS:
            _window_started = now
            _logged_keys.clear()
            _cap_reported = False
        if key in _logged_keys:
            return False
        if len(_logged_keys) >= MAX_KEYS_PER_WINDOW:
            if not _cap_reported:
                _cap_reported = True
                logger.warning(
                    "csp-violation limit reached: further distinct reports are not logged for up to %d seconds",
                    WINDOW_SECONDS,
                )
            return False
        _logged_keys.add(key)
        return True


class CspReportView(APIView):
    """Public endpoint for browsers' CSP violation reports: 204, or 413 for a body that is too large (429 when rate-limited)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "csp_reports"

    def post(self, request):
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_BODY_BYTES:
            return Response(status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        body = request.body
        if len(body) > MAX_BODY_BYTES:
            return Response(status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return Response(status=status.HTTP_204_NO_CONTENT)

        own_origins = {origin for origin in map(_origin, settings.CORS_ALLOWED_ORIGINS) if origin}
        browser = browser_from_user_agent(request.META.get("HTTP_USER_AGENT", ""))
        now = _now()
        for report in extract_reports(payload):
            summary = summarize(report, own_origins)
            if summary is None:
                continue
            summary["browser"] = browser
            if _should_log(tuple(summary.values()), now):
                logger.info(
                    'csp-violation disposition=%s directive=%s blocked=%s page=%s source=%s status=%s browser="%s"',
                    summary["disposition"],
                    summary["directive"],
                    summary["blocked"],
                    summary["page"],
                    summary["source"],
                    summary["status"],
                    summary["browser"],
                )
        return Response(status=status.HTTP_204_NO_CONTENT)
