from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from footboy.i18n import tr

# HLS forwards only nonempty HTTP options to its segment/key requests.
# FFmpeg only uses http:// proxy URLs; this non-HTTP marker forces direct access
# while surviving that propagation, unlike an empty http_proxy option.
DIRECT_HTTP_PROXY = "direct://"


@dataclass(slots=True)
class Source:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "unknown"
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    audio_codec: str | None = None
    has_audio: bool | None = None
    line_text: str | None = None
    no_proxy: bool = False

    @property
    def user_agent(self) -> str:
        return self.header("user-agent") or "Mozilla/5.0"

    @property
    def domain(self) -> str:
        return (urlparse(self.url).hostname or "unknown").lower()

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

    def cookie_header(self) -> str:
        parsed = urlparse(self.url)
        host, path = parsed.hostname or "", parsed.path or "/"

        def matches(item: dict[str, Any]) -> bool:
            domain = str(item.get("domain") or host).lstrip(".")
            cookie_path = str(item.get("path") or "/")
            return (
                (host == domain or host.endswith("." + domain))
                and (path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/"))
                and (not item.get("secure") or parsed.scheme == "https")
            )

        return "; ".join(
            f"{item['name']}={item['value']}"
            for item in self.cookies
            if item.get("name") and item.get("value") is not None and matches(item)
        )

    def ffmpeg_cookies(self) -> str:
        lines = []
        parsed = urlparse(self.url)
        for item in self.cookies:
            name, value = item.get("name"), item.get("value")
            if not name or value is None:
                continue
            path = str(item.get("path") or "/")
            domain = str(item.get("domain") or self.domain)
            lines.append(f"{name}={value}; path={path}; domain={domain};")
            host = (parsed.hostname or "").lower()
            cookie_domain = domain.lstrip(".").lower()
            if parsed.port is not None and (
                host == cookie_domain or host.endswith("." + cookie_domain)
            ):
                # libavformat matches cookies against the HTTP authority,
                # including an explicit port. Keep the ordinary domain entry
                # too, for redirects/segments that use the default authority.
                authority = (
                    f"[{domain}]" if ":" in domain and not domain.startswith("[") else domain
                )
                lines.append(f"{name}={value}; path={path}; domain={authority}:{parsed.port};")
        return "\r\n".join(lines) + ("\r\n" if lines else "")

    def ffmpeg_headers(self, *, include_cookies: bool = True) -> str:
        lines: list[str] = []
        for name, value in self.headers.items():
            if name.lower() in {"user-agent", "host", "content-length", "connection"}:
                continue
            if any(char in name + value for char in "\r\n"):
                raise ValueError(tr("Headers must not contain line breaks"))
            if value:
                lines.append(f"{name.title()}: {value}")
        cookie = self.cookie_header()
        if include_cookies and cookie and not self.header("cookie"):
            lines.append(f"Cookie: {cookie}")
        return "\r\n".join(lines) + ("\r\n" if lines else "")

    def pyav_options(self) -> dict[str, str]:
        options = {
            "headers": self.ffmpeg_headers(include_cookies=False),
            "cookies": self.ffmpeg_cookies(),
            "user_agent": self.user_agent,
            "rw_timeout": "15000000",
        }
        if self.kind == "hls":
            options["allowed_extensions"] = "ALL"
        options = {key: value for key, value in options.items() if value}
        if self.no_proxy:
            options["http_proxy"] = DIRECT_HTTP_PROXY
        return options

    def public_dict(self) -> dict[str, Any]:
        """Return diagnostics without leaking URLs, cookies, or signed tokens."""
        return {
            "domain": self.domain,
            "kind": self.kind,
            "video_codec": self.video_codec,
            "width": self.width,
            "height": self.height,
            "audio_codec": self.audio_codec,
            "has_audio": self.has_audio,
            "line_text": self.line_text,
            "no_proxy": self.no_proxy,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
