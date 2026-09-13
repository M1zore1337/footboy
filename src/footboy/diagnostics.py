from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from footboy.i18n import Message

_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_PRIVATE_HEADER = re.compile(
    r"(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*",
    re.IGNORECASE,
)


def redact_diagnostic(text: str) -> str:
    """Strip HTTP credentials before diagnostics reach logs or the control API."""
    if isinstance(text, Message):
        return Message(redact_diagnostic(text.english), redact_diagnostic(text.chinese))

    def url(match: re.Match[str]) -> str:
        try:
            parts = urlsplit(match[0])
        except ValueError:
            return "<redacted-url>"
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc.rsplit("@", 1)[-1],
                parts.path,
                "<redacted>" if parts.query else "",
                "<redacted>" if parts.fragment else "",
            )
        )

    return _PRIVATE_HEADER.sub(r"\1<redacted>", _URL.sub(url, text))
