# -*- coding: utf-8 -*-
"""Shared helpers: secret redaction, safe error text, atomic private JSON writes, input validators."""
import os, re, json, tempfile

_PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(access_token|auth_token|token|password|passwd|pwd|secret|api[_-]?key|key|client_secret)"
                r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;\"'}]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}"), "[REDACTED]"),   # long opaque blobs (tokens, keys, hex dumps)
]


def redact(text) -> str:
    """Remove anything that looks like a credential from a string before it is logged or shown."""
    s = str(text)
    for rx, repl in _PATTERNS:
        s = rx.sub(repl, s)
    return s


def safe_error(e: BaseException, limit: int = 200) -> str:
    """Exception -> short, redacted description (never the raw message of network errors, which can contain URLs)."""
    return f"{type(e).__name__}: {redact(e)[:limit]}"


def atomic_write_json(path, data, indent=2, private=True):
    """Write JSON via temp file + os.replace. Files holding credentials are chmod 0600 (best effort)."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        if private:
            try: os.chmod(tmp, 0o600)
            except OSError: pass
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---- validators ----
_UID_RE = re.compile(r"^\d{1,20}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-~+/=]{8,4096}$")
_REGION_RE = re.compile(r"^[A-Za-z]{2,8}$")


def valid_uid(v) -> bool:        return isinstance(v, str) and bool(_UID_RE.match(v))
def valid_ident(v) -> bool:      return isinstance(v, str) and bool(_IDENT_RE.match(v))
def valid_token(v) -> bool:      return isinstance(v, str) and bool(_TOKEN_RE.match(v))
def valid_region(v) -> bool:     return isinstance(v, str) and bool(_REGION_RE.match(v))
def valid_clan_id(v) -> bool:    return isinstance(v, str) and v.isdigit() and 1 <= len(v) <= 20
def valid_password(v) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= 128 and v.isprintable()
