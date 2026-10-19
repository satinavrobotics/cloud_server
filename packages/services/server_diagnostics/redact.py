"""Remove secrets from log lines before they leave the server.

Logs are shown to anyone who can open the dashboard, so every line is scrubbed on the way out: tokens, passwords,
API keys, credentials inside URLs. Redaction is best effort (a secret printed in free text cannot be recognized), which
is why the service is also reachable only over the tailnet.
"""

import re

REDACTED = "[redacted]"
MAX_LINE_CHARS = 2000

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^/\s@]+@", re.IGNORECASE)
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_SECRET_PAIR = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|authorization)[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)")


def redact_line(line: str) -> str:
    """One log line with secrets replaced by [redacted], cut to a sane length."""
    line = _URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", line)
    line = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", line)
    line = _JWT.sub(REDACTED, line)
    line = _AWS_KEY.sub(REDACTED, line)
    line = _SECRET_PAIR.sub(lambda m: m.group(1) + REDACTED, line)
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + " ...[cut]"
    return line
