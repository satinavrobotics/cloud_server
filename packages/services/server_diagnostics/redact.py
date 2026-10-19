"""Remove secrets from log lines before they leave the server.

Logs are shown to anyone who can open the dashboard, so every line is scrubbed on the way out: tokens, passwords,
API keys, cookies, credentials inside URLs. Redaction is best effort (a secret printed as free text cannot be recognized),
which is why the service is also reachable only through the gateway, over the tailnet, without CORS.

Every pattern is bounded (no unbounded prefix before a keyword) and the line is cut before any pattern runs, so a hostile
log line (a URL of 16 KB of "a-a-a-...", written by the access log) cannot make the scrub itself slow.
"""

import re

REDACTED = "[redacted]"
MAX_LINE_CHARS = 2000

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|token|basic|digest)\s+[A-Za-z0-9._~+/=-]{16,}|\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)(\"?\s*[:=]\s*)[^\r\n]*")
# scheme://user:password@host: the password runs to the LAST @ before the path, so a password containing : or @ goes whole
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]{1,20}://[^/\s:@]{0,64}:)[^/\s]{1,200}@(?=[^@/\s]*(?:[/\s?#]|$))", re.IGNORECASE)
_CURL_USER = re.compile(r"(?i)(\s(?:-u|--user)\s+[^\s:]{1,64}:)\S+")
_PROVIDER_KEY = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
                           r"AIza[0-9A-Za-z_-]{30,}|glpat-[A-Za-z0-9_-]{16,})\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]{1,30}PRIVATE KEY-----.*", re.IGNORECASE)
_KEYWORDS = (r"password|passwd|passphrase|pwd|pass|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|"
             r"authorization|auth|key|sk|sig|signature")
# key=value / "key": "value" / key: value; the key may carry a prefix and a suffix (aws_secret_access_key, MINIO_ROOT_PASSWORD)
_SECRET_PAIR = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]{0,40}(?:" + _KEYWORDS + r")[A-Za-z0-9_.-]{0,40}[\"']?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.){0,500}\"|'(?:[^'\\]|\\.){0,500}'|[^\s,;&}\]\"']{1,500})")
# prose: "password is hunter2", 'invalid password "hunter2"', "secret key abc123 rejected"
_SECRET_PROSE = re.compile(r"(?i)\b((?:" + _KEYWORDS + r")\s+(?:is|was|=|:)?\s*)(\"[^\"]{1,200}\"|'[^']{1,200}'|[^\s\"']{6,200})(?=\s|$)")


# aws_secret_access_key wJalr..., my_api_token abc123...: a snake_case identifier ending in a secret word, then the value
_SNAKE_KEY = re.compile(r"(?i)\b([A-Za-z0-9]{1,30}(?:[_-][A-Za-z0-9]{1,30}){0,6}[_-](?:key|secret|token|password|passwd)\s+)([^\s\"']{8,200})")


def redact_line(line: str) -> str:
    """One log line with secrets replaced by [redacted], cut to a sane length."""
    cut = len(line) > MAX_LINE_CHARS
    line = line[:MAX_LINE_CHARS]
    line = _PEM.sub("-----BEGIN PRIVATE KEY-----" + REDACTED, line)
    line = _COOKIE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, line)
    line = _URL_CREDENTIALS.sub(lambda m: m.group(1) + REDACTED + "@", line)
    line = _CURL_USER.sub(lambda m: m.group(1) + REDACTED, line)
    line = _AUTH_SCHEME.sub(lambda m: f"{m.group(1) or m.group(2)} {REDACTED}", line)
    line = _JWT.sub(REDACTED, line)
    line = _PROVIDER_KEY.sub(REDACTED, line)
    line = _SECRET_PAIR.sub(lambda m: m.group(1) + REDACTED, line)
    line = _SECRET_PROSE.sub(lambda m: m.group(1) + REDACTED, line)
    line = _SNAKE_KEY.sub(lambda m: m.group(1) + REDACTED, line)
    return line + " ...[cut]" if cut else line
