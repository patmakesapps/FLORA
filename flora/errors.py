"""Diagnostics that may be logged without exposing requests or credentials."""
import re


class SessionError(RuntimeError):
    """A locally constructed, safe-to-log session failure."""


def error_code(error):
    code = error.get("code") or error.get("type")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code) else "unknown"


def failure_reason(exc):
    return str(exc) if isinstance(exc, SessionError) else type(exc).__name__
