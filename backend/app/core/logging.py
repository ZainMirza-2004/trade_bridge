import logging
import re

_SECRET_PATTERNS = [
    re.compile(r"(X-Revx-(?:API-Key|Signature)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


class RedactingFilter(logging.Filter):
    """Last line of defence: scrub anything that looks like a credential from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = msg
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request lines at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
