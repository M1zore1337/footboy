"""Resolve live rooms to a single HTTP media source usable by libavformat.

Streamlink handles platform signing and yt-dlp broadens extractor coverage. We
only export ordinary HTTP/HLS streams: special readers, separate DASH tracks,
and streams requiring a downloader-side muxer cannot be played from a URL alone.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import parse_qs, urlsplit

from footboy.diagnostics import redact_diagnostic
from footboy.i18n import exception_message, tr

from .bili import DEFAULT_UA, BiliResolver
from .media_probe import MediaProbeError
from .models import Source, merge_headers

_DIRECT_KINDS = {
    ".m3u8": "hls",
    ".flv": "flv",
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".ts": "mpegts",
    ".mts": "mpegts",
    ".webm": "webm",
    ".mkv": "matroska",
}
_AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_HTTP_PROTOCOLS = {"http", "https", "m3u8", "m3u8_native"}
_VIDEO_CODECS = ("avc", "h264", "hvc", "hev", "h265", "vp8", "vp9", "vp09", "av01", "dvh")
_AUDIO_CODECS = ("mp4a", "aac", "ac-3", "ec-3", "opus", "vorbis", "mp3")


class LiveResolveError(RuntimeError):
    pass


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.port != 0
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
    except (TypeError, ValueError):
        return False


def direct_kind(url: str) -> str | None:
    """Identify common video/media URLs without treating webpage query values as URLs."""
    if not _is_http_url(url):
        return None
    return _DIRECT_KINDS.get(Path(urlsplit(url).path.lower()).suffix)


def _cookie_jar(cookies_file: Path | None) -> MozillaCookieJar:
    jar = MozillaCookieJar(str(cookies_file) if cookies_file else None)
    if cookies_file:
        try:
            with warnings.catch_warnings():
                # cookiejar warns with the malformed cookie's traceback.
                warnings.simplefilter("ignore", UserWarning)
                jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                # Browser/Netscape exports commonly encode session cookies as 0.
                if cookie.expires == 0:
                    cookie.expires, cookie.discard = None, True
                elif cookie.is_expired():
                    jar.clear(cookie.domain, cookie.path, cookie.name)
        except (OSError, ValueError):
            # LoadError may contain an entire malformed cookie line.
            raise LiveResolveError(tr("Cannot read the Netscape cookies file")) from None
    return jar


def _cookies_from_jar(jar: Iterable[Cookie]) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        if cookie.is_expired():
            continue
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": cookie.secure,
        }
        if cookie.expires is not None:
            item["expires"] = cookie.expires
        cookies.append(item)
    return cookies


def cookies_as_dicts(cookies_file: Path | None) -> list[dict[str, Any]]:
    """Read Netscape cookies with their scope intact for media and browser requests."""
    return _cookies_from_jar(_cookie_jar(cookies_file))


def _merge_cookies(cookies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(
        {
            (cookie.get("domain", ""), cookie.get("path", "/"), cookie["name"]): cookie
            for cookie in cookies
        }.values()
    )


def _room_url(url: str) -> str:
    """Normalize common mobile/share URLs to the corresponding desktop room."""
    parts = urlsplit(url)
    host = parts.hostname
    if host in {"m.douyu.com", "m.huya.com"} and not parts.username:
        host = "www." + host[2:]
        authority = host + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=authority)
    if host in {"douyu.com", "www.douyu.com"} and parts.path.startswith("/topic/"):
        room_id = parse_qs(parts.query).get("rid", [""])[0]
        if re.fullmatch(r"\d+", room_id):
            parts = parts._replace(path=f"/{room_id}")
    return parts.geturl()


def _media_headers(*mappings: Mapping[str, Any] | None) -> dict[str, str]:
    # The downloader's compression and connection negotiation is not transferable
    # to libavformat. Cookies from a jar are transferred separately with scope.
    return {
        key: value
        for key, value in merge_headers(*mappings).items()
        if key.lower() not in {"accept-encoding", "host", "content-length", "connection"}
    }


def _validated(source: Source, validate: Callable[[Source], Source] | None) -> Source:
    if not _is_http_url(source.url):
        raise LiveResolveError(tr("The extractor returned an invalid media URL"))
    if source.has_audio is False:
        raise LiveResolveError(tr("The commentary stream has no audio"))
    if validate is not None:
        try:
            return validate(source)
        except MediaProbeError as exc:
            raise LiveResolveError(redact_diagnostic(exception_message(exc))) from None
        except Exception as exc:
            # Media probes and external validators may include complete argv/URLs.
            raise LiveResolveError(
                tr("Media validation failed ({0})", type(exc).__name__)
            ) from None
    return source


@dataclass(slots=True)
class LiveResolver:
    cookies_file: Path | None = None
    headers: dict[str, str] | None = None
    stop_event: Event | None = None

    def _check_cancelled(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise LiveResolveError(tr("Live source resolution was cancelled"))

    def resolve(
        self,
        room_url: str,
        *,
        validate: Callable[[Source], Source] | None = None,
    ) -> Source:
        self._check_cancelled()
        room_url = room_url.strip()
        if not _is_http_url(room_url):
            raise LiveResolveError(tr("Enter an HTTP(S) live room or media URL"))
        if any(
            char in str(name) + str(value)
            for name, value in (self.headers or {}).items()
            for char in "\r\n"
        ):
            raise LiveResolveError(tr("Headers must not contain line breaks"))

        # Read first, so malformed cookies are reported instead of being hidden
        # behind multiple extractor failures. This also covers direct media URLs.
        cookies = cookies_as_dicts(self.cookies_file)
        kind = direct_kind(room_url)
        if kind:
            return _validated(
                Source(
                    room_url,
                    headers=merge_headers({"User-Agent": DEFAULT_UA}, self.headers),
                    cookies=cookies,
                    kind=kind,
                ),
                validate,
            )

        room_url = _room_url(room_url)
        backends: list[tuple[str, Callable[[str], Source]]] = []
        if urlsplit(room_url).hostname == "live.bilibili.com":
            backends.append(("Bilibili", self._resolve_bili))
        backends.extend((("Streamlink", self._resolve_streamlink), ("yt-dlp", self._resolve_ytdlp)))
        errors: list[str] = []
        for name, resolve in backends:
            self._check_cancelled()
            try:
                source = resolve(room_url)
                self._check_cancelled()
                source = _validated(source, validate)
                self._check_cancelled()
                if urlsplit(room_url).hostname in {"douyu.com", "www.douyu.com"}:
                    # Douyu signs each playback connection. Reusing the URL
                    # after probing, or sharing it between OCR and the muxer,
                    # can truncate the next reader's FLV response mid-packet.
                    # Each reader needs a new URL on the same source timeline.
                    source = replace(source, reader_factory=lambda: self.resolve(room_url))
                return source
            except LiveResolveError as exc:
                self._check_cancelled()
                errors.append(tr("{0}: {1}", name, exception_message(exc)))
            except Exception as exc:
                self._check_cancelled()
                # Never surface backend exception text: it commonly contains
                # signed URLs, Cookie headers, or authentication response bodies.
                errors.append(tr("{0} failed ({1})", name, type(exc).__name__))
        raise LiveResolveError(
            tr("Cannot resolve this live commentary source: {0}", tr("; ").join(errors))
        ) from None

    def _resolve_bili(self, room_url: str) -> Source:
        return BiliResolver(cookies_file=self.cookies_file, headers=self.headers).resolve(room_url)

    def _resolve_streamlink(self, room_url: str) -> Source:
        try:
            from streamlink import Streamlink
            from streamlink.exceptions import NoPluginError
            from streamlink.stream.hls import HLSStream
            from streamlink.stream.http import HTTPStream
        except ImportError:
            raise LiveResolveError(tr("Streamlink is not installed")) from None

        session = Streamlink()
        try:
            session.set_option("http-timeout", 15)
            session.set_option("stream-timeout", 15)
            session.http.headers.update(
                merge_headers({"User-Agent": DEFAULT_UA, "Referer": room_url}, self.headers)
            )
            if self.headers:
                prepare_request = session.http.prepare_request

                def prepare_with_overrides(request: Any) -> Any:
                    # Plugins can set their own request headers after session
                    # creation. Explicit user overrides still take precedence.
                    request.headers = merge_headers(request.headers, self.headers)
                    return prepare_request(request)

                session.http.prepare_request = prepare_with_overrides
            session.http.cookies.update(_cookie_jar(self.cookies_file))
            try:
                streams = session.streams(room_url)
            except NoPluginError:
                raise LiveResolveError(tr("No Streamlink plugin supports this URL")) from None
            self._check_cancelled()
            if not streams:
                raise LiveResolveError(
                    tr(
                        "Streamlink found no live streams (the room may be offline or require login)"
                    )
                )

            candidates: list[tuple[tuple[int, int, float], Source]] = []
            seen: set[int] = set()
            for name, stream in streams.items():
                if id(stream) in seen:
                    continue
                seen.add(id(stream))
                names = [label for label, value in streams.items() if value is stream]
                if any("audio" in label.lower() for label in names):
                    continue
                # Even HLS subclasses may rewrite manifests or decrypt custom
                # segments. Their raw URL is not a substitute for their reader.
                if type(stream) not in {HTTPStream, HLSStream}:
                    continue
                if getattr(stream, "force_restart", False):
                    continue
                request = session.http.prepare_new_request(**stream.args)
                if request.method != "GET" or request.body is not None:
                    continue
                url = request.url or ""
                if not _is_http_url(url):
                    continue
                suffix = Path(urlsplit(url).path.lower()).suffix
                if suffix in _AUDIO_SUFFIXES or suffix in {".mpd", ".f4m", ".ism"}:
                    continue

                stream_headers = _media_headers(request.headers)
                explicit = merge_headers(session.http.headers, stream.args.get("headers"))
                if not any(key.lower() == "cookie" for key in explicit):
                    stream_headers = {
                        key: value
                        for key, value in stream_headers.items()
                        if key.lower() != "cookie"
                    }
                cookies = _cookies_from_jar(session.http.cookies)
                extra_cookies = stream.args.get("cookies")
                if isinstance(extra_cookies, Mapping):
                    cookies.extend(
                        {"name": key, "value": value, "domain": urlsplit(url).hostname, "path": "/"}
                        for key, value in extra_cookies.items()
                    )
                elif extra_cookies is not None:
                    cookies.extend(_cookies_from_jar(extra_cookies))

                source = Source(
                    url,
                    headers=merge_headers(
                        {"User-Agent": DEFAULT_UA, "Referer": room_url},
                        stream_headers,
                        self.headers,
                    ),
                    cookies=_merge_cookies(cookies),
                    kind="hls" if type(stream) is HLSStream else direct_kind(url) or "http",
                )
                if type(stream) is HLSStream and not _hls_metadata(stream, source):
                    continue
                height_match = re.search(r"(?:^|_)(\d{3,4})p", name)
                height = source.height or (int(height_match[1]) if height_match else 0)
                bitrate_match = re.search(r"(?:^|_)(\d+)k", name)
                bitrate = float(bitrate_match[1]) if bitrate_match else 0
                rank = (int(height <= 1080), int("best" in names), height or bitrate)
                candidates.append((rank, source))
            if not candidates:
                raise LiveResolveError(
                    tr("Streamlink found no directly playable video and audio stream")
                )
            return max(candidates, key=lambda item: item[0])[1]
        finally:
            session.http.close()

    def _resolve_ytdlp(self, room_url: str) -> Source:
        try:
            import yt_dlp
        except ImportError:
            raise LiveResolveError(tr("yt-dlp is not installed")) from None

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": _QuietLogger(),
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 1,
            "extractor_retries": 1,
            "http_headers": merge_headers(
                {"User-Agent": DEFAULT_UA, "Referer": room_url}, self.headers
            ),
        }
        with yt_dlp.YoutubeDL(options) as downloader:  # pyright: ignore[reportArgumentType]
            # Loading into the jar avoids yt-dlp rewriting the user's cookiefile
            # on context-manager exit while still retaining cookies set by sites.
            for cookie in _cookie_jar(self.cookies_file):
                downloader.cookiejar.set_cookie(cookie)
            info = downloader.extract_info(room_url, download=False)
            cookies = _cookies_from_jar(downloader.cookiejar)
        self._check_cancelled()

        if not isinstance(info, dict):
            raise LiveResolveError(tr("yt-dlp returned no live stream information"))
        if info.get("is_live") is not True and info.get("live_status") != "is_live":
            raise LiveResolveError(tr("The room is offline or playing a rerun"))
        formats = [item for item in info.get("formats") or [] if isinstance(item, dict)]
        if info.get("url"):
            formats.append(info)
        candidates = [
            item for item in formats if _playable_format(item) and not info.get("has_drm")
        ]
        if not candidates:
            raise LiveResolveError(
                tr("yt-dlp found no directly playable live video and audio format")
            )
        chosen = max(candidates, key=_format_rank)
        headers = merge_headers(
            {"User-Agent": DEFAULT_UA, "Referer": room_url},
            _media_headers(info.get("http_headers"), chosen.get("http_headers")),
            self.headers,
        )
        protocol = str(chosen.get("protocol") or "").lower()
        url = str(chosen["url"])
        return Source(
            url,
            headers=headers,
            cookies=cookies,
            kind=(
                "hls"
                if protocol in {"m3u8", "m3u8_native"}
                else direct_kind(url) or str(chosen.get("ext") or "http")
            ),
            video_codec=_codec(chosen.get("vcodec")),
            audio_codec=_codec(chosen.get("acodec")),
            width=int(_number(chosen.get("width"))) or None,
            height=int(_number(chosen.get("height"))) or None,
            has_audio=True if _codec(chosen.get("acodec")) else None,
        )


def _hls_metadata(stream: Any, source: Source) -> bool:
    # Streamlink 7 calls this "master"; Streamlink 8 calls it "multivariant".
    master = getattr(stream, "multivariant", None) or getattr(stream, "master", None)
    for playlist in getattr(master, "playlists", []):
        if playlist.uri not in {stream.args.get("url"), source.url}:
            continue
        if playlist.is_iframe or any(
            media.type == "AUDIO" and media.uri for media in playlist.media
        ):
            return False
        info = playlist.stream_info
        codecs = [str(codec).lower() for codec in info.codecs]
        video = next((codec for codec in codecs if codec.startswith(_VIDEO_CODECS)), None)
        audio = next((codec for codec in codecs if codec.startswith(_AUDIO_CODECS)), None)
        if (video and not audio) or (audio and not video):
            return False
        source.video_codec, source.audio_codec = video, audio
        if audio:
            source.has_audio = True
        if info.resolution:
            source.width, source.height = info.resolution.width, info.resolution.height
        break
    return True


def _codec(value: Any) -> str | None:
    return None if value in {None, "", "none", "unknown"} else str(value)


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if 0 < number < float("inf") else 0
    except (ValueError, TypeError):
        return 0


def _playable_format(item: dict[str, Any]) -> bool:
    url = str(item.get("url") or "")
    if not _is_http_url(url) or item.get("has_drm"):
        return False
    if item.get("acodec") == "none" or item.get("vcodec") == "none":
        return False
    if item.get("audio_channels") == 0:
        return False
    if any(
        item.get(key)
        for key in (
            "requested_formats",
            "fragments",
            "extra_param_to_segment_url",
            "extra_param_to_key_url",
        )
    ):
        return False
    protocol = str(item.get("protocol") or "").lower()
    if protocol and protocol not in _HTTP_PROTOCOLS:
        return False
    suffix = Path(urlsplit(url).path.lower()).suffix
    ext = str(item.get("ext") or "").lower()
    if suffix in _AUDIO_SUFFIXES or suffix in {".mpd", ".f4m", ".ism"}:
        return False
    if ext in {"mpd", "f4m", "ism", "mhtml"}:
        return False
    marker = " ".join(str(item.get(key) or "") for key in ("resolution", "format_note"))
    return "audio only" not in marker.lower() and "video only" not in marker.lower()


def _format_rank(item: dict[str, Any]) -> tuple[bool, bool, float, bool, float]:
    height = _number(item.get("height"))
    av_known = bool(_codec(item.get("acodec")) and _codec(item.get("vcodec")))
    avc = str(item.get("vcodec") or "").startswith(("avc", "h264"))
    return height <= 1080, av_known, height, avc, _number(item.get("tbr"))


class _QuietLogger:
    """yt-dlp errors can print credentials even with quiet=True."""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass
