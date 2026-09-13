from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

from footboy.i18n import tr

from .models import Source

API_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class BiliResolveError(RuntimeError):
    pass


@dataclass(slots=True)
class BiliResolver:
    cookies_file: Path | None = None

    def resolve(self, room_url: str) -> Source:
        _room_id(room_url)
        errors: list[str] = []
        try:
            return self._resolve_ytdlp(room_url)
        except Exception as exc:  # yt-dlp extractors fail in many site-specific ways
            errors.append(tr("yt-dlp: {0}", exc))
        try:
            return self._resolve_api(room_url)
        except Exception as exc:
            errors.append(tr("Bilibili API: {0}", exc))
        raise BiliResolveError(
            tr("Cannot resolve the Bilibili live stream; ") + tr("; ").join(errors)
        )

    def _resolve_ytdlp(self, room_url: str) -> Source:
        try:
            import yt_dlp
        except ImportError as exc:
            raise BiliResolveError(tr("yt-dlp is not installed")) from exc

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 1,
            "extractor_retries": 1,
        }
        if self.cookies_file:
            options["cookiefile"] = str(self.cookies_file)
        with yt_dlp.YoutubeDL(options) as downloader:  # pyright: ignore[reportArgumentType]
            info = downloader.extract_info(room_url, download=False)
        if not isinstance(info, dict):
            raise BiliResolveError(tr("yt-dlp returned no live stream information"))
        if info.get("is_live") is not True and info.get("live_status") != "is_live":
            raise BiliResolveError(tr("The room is offline or playing a rerun"))

        formats = [item for item in (info.get("formats") or []) if isinstance(item, dict)]
        if info.get("url"):
            formats.append(dict(info))
        candidates = []
        for item in formats:
            url = str(item.get("url") or "")
            marker = " ".join(
                str(item.get(key) or "")
                for key in ("ext", "protocol", "format_id", "format", "url")
            ).lower()
            height = item.get("height")
            if not url or "flv" not in marker:
                continue
            if isinstance(height, (int, float)) and height > 1080:
                continue
            candidates.append(item)
        if not candidates:
            raise BiliResolveError(tr("yt-dlp found no live FLV format at 1080p or below"))
        chosen = max(
            candidates,
            key=lambda item: (
                int(item.get("height") or 0),
                float(item.get("tbr") or 0),
            ),
        )
        headers = {
            str(key): str(value)
            for key, value in (chosen.get("http_headers") or info.get("http_headers") or {}).items()
        }
        headers.setdefault("Referer", room_url)
        headers.setdefault("User-Agent", DEFAULT_UA)
        return Source(
            url=str(chosen["url"]),
            headers=headers,
            cookies=self._cookies_as_dicts(),
            kind="flv",
            video_codec=_clean_codec(chosen.get("vcodec")),
            width=int(chosen["width"]) if chosen.get("width") else None,
            height=int(chosen["height"]) if chosen.get("height") else None,
            audio_codec=_clean_codec(chosen.get("acodec")),
            has_audio=chosen.get("acodec") not in (None, "none"),
        )

    def _resolve_api(self, room_url: str) -> Source:
        room_id = _room_id(room_url)
        params = urllib.parse.urlencode(
            {
                "room_id": room_id,
                "protocol": "0,1",
                "format": "0,1,2",
                "codec": "0,1",
                "qn": "10000",
                "platform": "web",
                "ptype": "8",
            }
        )
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": f"https://live.bilibili.com/{room_id}",
        }
        request = urllib.request.Request(f"{API_URL}?{params}", headers=headers)
        jar = self._cookie_jar()
        if jar is not None:
            jar.add_cookie_header(request)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise BiliResolveError(tr("Request failed: {0}", exc)) from exc
        if payload.get("code") != 0:
            raise BiliResolveError(str(payload.get("message") or payload.get("code")))
        data = payload.get("data") or {}
        if data.get("live_status", (data.get("room_info") or {}).get("live_status")) != 1:
            raise BiliResolveError(tr("The room is offline or playing a rerun"))

        playurl = (data.get("playurl_info") or {}).get("playurl") or {}
        streams = playurl.get("stream") or []
        candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for stream in streams:
            protocol = str(stream.get("protocol_name") or "")
            for fmt in stream.get("format") or []:
                format_name = str(fmt.get("format_name") or "")
                if "flv" not in f"{protocol} {format_name}".lower():
                    continue
                for codec in fmt.get("codec") or []:
                    qn = int(codec.get("current_qn") or 0)
                    codec_name = str(codec.get("codec_name") or "")
                    avc_first = 1 if codec_name in {"avc", "h264"} else 0
                    if codec.get("base_url") and codec.get("url_info"):
                        candidates.append((qn, avc_first, codec, fmt))
        if not candidates:
            raise BiliResolveError(tr("The API returned no usable FLV URL"))
        _, _, codec, _ = max(candidates, key=lambda row: (row[0], row[1]))
        url_info = codec["url_info"][0]
        direct_url = f"{url_info.get('host', '')}{codec['base_url']}{url_info.get('extra', '')}"
        if not direct_url.startswith(("http://", "https://")):
            raise BiliResolveError(tr("The API returned an invalid stream URL"))
        return Source(
            url=direct_url,
            headers=headers,
            cookies=self._cookies_as_dicts(),
            kind="flv",
            video_codec=str(codec.get("codec_name") or "") or None,
            has_audio=True,
        )

    def _cookie_jar(self) -> MozillaCookieJar | None:
        if not self.cookies_file:
            return None
        jar = MozillaCookieJar(str(self.cookies_file))
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as exc:
            raise BiliResolveError(tr("Cannot read the Netscape cookies file: {0}", exc)) from exc
        return jar

    def _cookies_as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            }
            for cookie in (self._cookie_jar() or [])
        ]


def _room_id(room_url: str) -> str:
    parts = urllib.parse.urlsplit(room_url)
    match = re.fullmatch(r"/(?:blanc/)?(\d+)/?", parts.path)
    if parts.scheme not in {"http", "https"} or parts.hostname != "live.bilibili.com":
        raise BiliResolveError(tr("Enter a live.bilibili.com room URL"))
    if not match:
        raise BiliResolveError(tr("Invalid Bilibili room URL"))
    return match.group(1)


def _clean_codec(value: Any) -> str | None:
    return None if value in (None, "none") else str(value)
