from __future__ import annotations

import math
import os
import queue
import re
import select
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from footboy.i18n import tr

from .lines import LINE_SELECTOR, is_line_label, normalize_line_text, validate_line_text
from .media_probe import MediaProbeError, ffprobe_source
from .models import Source

PLAYLIST_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
}
FLV_TYPES = {"video/x-flv"}
SEGMENT_SUFFIXES = (".ts", ".m4s", ".mp4", ".aac", ".m4a")


class SniffError(RuntimeError):
    pass


@dataclass(slots=True)
class Candidate:
    url: str
    kind: str
    headers: dict[str, str]
    first_seen: float
    last_seen: float
    last_segment_at: float = 0.0
    stable_since: float | None = None
    cancelled_until: float = 0.0
    line_text: str | None = None
    identifier: int = 0
    segments: set[str] = field(default_factory=set)
    request_open: bool = False
    browser_blocked: bool = False
    segment_duration: float = 2.0
    last_segment_key: tuple[str, str | None] | None = None
    last_progress_at: float = 0.0

    def active(self, now: float) -> bool:
        # A continuously downloaded FLV has no child segment requests; its own
        # response stays open, so there will not be periodic response events.
        if self.kind == "flv":
            return self.request_open
        last_activity = self.last_segment_at
        return last_activity > 0 and now - last_activity <= max(3.0, 2 * self.segment_duration)

    def observe_segment(self, url: str, now: float, byte_range: str | None = None) -> None:
        if now < self.last_segment_at:
            return
        key = (url, byte_range)
        if key != self.last_segment_key:
            self.last_segment_key = key
            self.last_progress_at = now
        self.last_segment_at = now

    def ready_for_auto_select(self, now: float) -> bool:
        if self.stable_since is None or now - self.stable_since < 10:
            return False
        # A longer activity window must not turn a single downloaded segment
        # (or repeated retries of that segment) into proof of live playback.
        return (
            self.kind == "flv" or self.browser_blocked or self.last_progress_at > self.stable_since
        )

    @property
    def display_url(self) -> str:
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class StreamSniffer:
    def __init__(
        self, *, ffprobe: str | Path = "ffprobe", timeout: float = 300, no_proxy: bool = False
    ) -> None:
        self.ffprobe = ffprobe
        self.timeout = timeout
        self.no_proxy = no_proxy
        self._candidates: dict[str, Candidate] = {}
        self._lock = threading.RLock()
        self._commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._line_commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._stop_keyboard = threading.Event()
        self._abort = threading.Event()
        self._page: Any = None
        self._context: Any = None
        self._recent_requests: dict[str, tuple[float, str | None]] = {}
        self._started = 0.0
        self._lines: list[str] = []
        self._selected_line: str | None = None
        self._pending_line: str | None = None
        self._auto_select = True
        self._generation = 0
        self._next_identifier = 1
        self._request_generations: dict[int, int] = {}
        self._blocked_requests: list[tuple[str, dict[str, str], int, str | None]] = []
        self._blocked_seen: set[str] = set()
        self.running = False
        self.message = ""

    def sniff(
        self,
        page_url: str,
        *,
        preferred_line_text: str | None = None,
        headless: bool = False,
        auto_select: bool = True,
    ) -> Source:
        if self._abort.is_set():
            raise SniffError(tr("Stream discovery cancelled"))
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SniffError(
                tr(
                    "Playwright is not installed; install the dependencies and run playwright install chromium"
                )
            ) from exc

        self._candidates.clear()
        self._recent_requests.clear()
        self._commands = queue.SimpleQueue()
        self._line_commands = queue.SimpleQueue()
        self._request_generations.clear()
        self._blocked_requests.clear()
        self._blocked_seen.clear()
        self._lines = []
        self._selected_line = None
        self._pending_line = validate_line_text(preferred_line_text, optional=True)
        self._auto_select = auto_select
        self._generation += 1
        self.message = tr("Looking for streams on the page")
        self._started = time.monotonic()
        self.running = True
        self._stop_keyboard.clear()
        keyboard = threading.Thread(
            target=self._keyboard_loop, name="sniffer-keyboard", daemon=True
        )
        keyboard.start()
        try:
            with sync_playwright() as playwright:
                browser = self._launch_browser(
                    playwright, PlaywrightError, headless=headless, no_proxy=self.no_proxy
                )
                try:
                    self._context = browser.new_context()
                    self._page = self._context.new_page()
                    main_page = self._page
                    self._context.on("page", lambda page: self._close_popup(page, main_page))
                    self._context.on("response", self._on_response)
                    self._context.on("request", self._on_request)
                    self._context.on("requestfinished", self._on_request_finished)
                    self._context.on("requestfailed", self._on_request_failed)
                    self._context.expose_binding("__footboy_line_clicked", self._on_line_click)
                    self._page.add_init_script(
                        """
                        document.addEventListener('click', event => {
                          if (!event.isTrusted) return;
                          const el = event.target && event.target.closest('button,a,[role=button],[onclick],[data-play]');
                          if (el) {
                            const text = (el.innerText || el.textContent || '').trim();
                            window.__footboy_last_click = text;
                            window.__footboy_line_clicked(text).catch(() => {});
                          }
                        }, true);
                        """
                    )
                    try:
                        response = self._page.goto(
                            page_url, wait_until="domcontentloaded", timeout=15_000
                        )
                        if response is not None and response.status >= 400:
                            raise SniffError(
                                tr(
                                    "The match page denied access (HTTP {0}); cannot list streams",
                                    response.status,
                                )
                            )
                    except PlaywrightError as exc:
                        print(
                            tr(
                                "The page did not finish loading; continuing stream discovery: {0}",
                                exc,
                            ),
                            file=sys.stderr,
                        )
                    return self._selection_loop()
                finally:
                    browser.close()
        finally:
            self._stop_keyboard.set()
            keyboard.join(timeout=0.5)
            self.running = False
            self._page = None
            self._context = None

    def stop(self) -> None:
        """Ask an in-progress interactive sniff to close at its next event tick."""
        self._abort.set()

    def select(self, identifier: int | None) -> None:
        if not self.running:
            raise ValueError(tr("No stream discovery is in progress"))
        self._commands.put(str(identifier) if identifier is not None else "c")

    def select_line(self, text: str) -> None:
        text = validate_line_text(text)
        if not self.running:
            raise ValueError(tr("No stream discovery is in progress"))
        assert text is not None
        with self._lock:
            self._pending_line = text  # Also invalidate a probe already running on another line.
            self._line_commands.put(text)

    def public_status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {
                "running": self.running,
                "message": self.message,
                "lines": list(self._lines),
                "selected_line": self._selected_line,
                "pending_line": self._pending_line,
                "candidates": [
                    {
                        "id": item.identifier,
                        "url": item.display_url,
                        "kind": item.kind,
                        "line_text": item.line_text,
                        "browser_blocked": item.browser_blocked,
                        "active": item.active(now),
                        "cancelled": item.cancelled_until > now,
                        "countdown": max(0, round(10 - (now - item.stable_since)))
                        if item.stable_since is not None
                        else None,
                    }
                    for item in self._ranked(now)
                ],
            }

    @staticmethod
    def _launch_browser(
        playwright: Any,
        error_type: type[Exception],
        *,
        headless: bool = False,
        no_proxy: bool = False,
    ) -> Any:
        args = ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]
        if no_proxy:
            args.append("--no-proxy-server")
        failures = []
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = {"headless": headless, "args": args}
                if channel:
                    kwargs["channel"] = channel
                return playwright.chromium.launch(**kwargs)
            except error_type as exc:
                failures.append(f"{channel or 'chromium'}: {exc}")
        raise SniffError(
            tr("Could not start Chrome, Edge, or Playwright Chromium: ") + tr("; ").join(failures)
        )

    @staticmethod
    def _close_popup(page: Any, main_page: Any) -> None:
        if page is not main_page:
            try:
                page.close()
            except Exception:
                pass

    def _discover_lines(self) -> None:
        if self._page is None:
            return
        found: dict[str, str] = {}
        for frame in self._page.frames:
            try:
                labels = frame.locator(LINE_SELECTOR).evaluate_all(
                    """elements => elements.filter(el => el.getClientRects().length)
                      .map(el => (el.innerText || el.textContent || '').trim())
                      .filter(text => text.length > 0 && text.length <= 80)"""
                )
                for label in labels:
                    if is_line_label(label) or (
                        self._pending_line
                        and normalize_line_text(label) == normalize_line_text(self._pending_line)
                    ):
                        found.setdefault(normalize_line_text(label), label)
            except Exception:
                continue  # A player frame can be replaced while changing lines.
        with self._lock:
            self._lines = list(found.values())

    def _activate_line(self, text: str, *, force: bool = False) -> None:
        with self._lock:
            if force or normalize_line_text(text) != normalize_line_text(self._selected_line or ""):
                self._generation += 1
                self._candidates.clear()
                self._recent_requests.clear()
                self._blocked_requests.clear()
                self._blocked_seen.clear()
            self._selected_line = text
            self._pending_line = None
            self._auto_select = True
            self.message = tr("Fetching stream: {0}", text)

    def _on_line_click(self, _source: Any, text: str) -> None:
        if is_line_label(text) or (
            self._pending_line
            and normalize_line_text(text) == normalize_line_text(self._pending_line)
        ):
            self._activate_line(text)

    def _reuse_line(self, text: str) -> bool:
        assert self._page is not None
        wanted = normalize_line_text(text)
        labels = [label for label in self._lines if normalize_line_text(label) == wanted]
        for frame in self._page.frames:
            for label in labels or [text]:
                try:
                    locator = frame.get_by_text(label, exact=True)
                    for index in range(locator.count()):
                        item = locator.nth(index)
                        if item.is_visible():
                            self._activate_line(label, force=True)
                            item.click(timeout=2_000)
                            return True
                except Exception:
                    self._pending_line = text
        self.message = tr(
            'Could not find or click stream "{0}"; choose another in the page or console', text
        )
        return False

    def _on_response(self, response: Any) -> None:
        generation = self._request_generations.get(id(response.request), self._generation)
        if generation != self._generation or self._pending_line:
            return
        if response.status < 200 or response.status >= 300:
            return
        now = time.monotonic()
        url = response.url
        content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].lower()
        lower_url = urlsplit(url).path.lower()

        kind: str | None = None
        segments: set[str] = set()
        segment_duration = 2.0
        if ".m3u8" in lower_url or content_type in PLAYLIST_TYPES:
            try:
                body = response.body().decode("utf-8", errors="replace")
            except Exception:
                return
            if not _live_playlist(body):
                return
            # A master manifest is not itself playing. Only media playlists
            # with exact child requests can become an active candidate.
            if "#EXT-X-STREAM-INF" in body or "#EXT-X-I-FRAME-STREAM-INF" in body:
                return
            kind = "hls"
            segments = _playlist_segments(url, body)
            segment_duration = _playlist_segment_duration(body)
        elif ".flv" in lower_url or content_type in FLV_TYPES:
            kind = "flv"

        if kind:
            headers = _request_headers(response.request)
            with self._lock:
                if generation != self._generation or self._pending_line:
                    return
                candidate = self._candidates.get(url)
                if candidate:
                    candidate.last_seen = now
                    candidate.headers.update(headers)
                else:
                    candidate = Candidate(
                        url=url,
                        kind=kind,
                        headers=headers,
                        first_seen=now,
                        last_seen=now,
                        line_text=self._selected_line or self._last_click_text(),
                        identifier=self._next_identifier,
                    )
                    self._next_identifier += 1
                    self._candidates[url] = candidate
                candidate.segments = segments
                candidate.request_open = kind == "flv"
                candidate.browser_blocked = False
                candidate.segment_duration = segment_duration
                for when, segment, byte_range in sorted(
                    (
                        self._recent_requests[segment][0],
                        segment,
                        self._recent_requests[segment][1],
                    )
                    for segment in segments
                    if segment in self._recent_requests
                ):
                    candidate.observe_segment(segment, when, byte_range)

    def _on_request(self, request: Any) -> None:
        now = time.monotonic()
        byte_range = next(
            (
                value
                for name, value in getattr(request, "headers", {}).items()
                if name.lower() == "range"
            ),
            None,
        )
        with self._lock:
            self._request_generations[id(request)] = self._generation
            self._recent_requests[request.url] = (now, byte_range)
            if len(self._recent_requests) > 500:
                self._recent_requests = {
                    url: activity
                    for url, activity in self._recent_requests.items()
                    if now - activity[0] < 20
                }
            for candidate in self._candidates.values():
                if request.url in candidate.segments:
                    candidate.observe_segment(request.url, now, byte_range)

    def _on_request_finished(self, request: Any) -> None:
        with self._lock:
            generation = self._request_generations.pop(id(request), self._generation)
            if generation != self._generation:
                return
            candidate = self._candidates.get(request.url)
            if candidate and candidate.kind == "flv":
                candidate.request_open = False

    def _on_request_failed(self, request: Any) -> None:
        generation = self._request_generations.get(id(request), self._generation)
        self._on_request_finished(request)
        failure = (getattr(request, "failure", "") or "").lower().replace("_", "-")
        path = urlsplit(request.url).path.lower()
        if (
            "mixed-content" not in failure
            or not path.endswith((".m3u8", ".flv"))
            or generation != self._generation
            or self._pending_line
            or request.url in self._blocked_seen
        ):
            return
        self._blocked_seen.add(request.url)
        self._blocked_requests.append(
            (request.url, _request_headers(request), generation, self._selected_line)
        )

    def _read_blocked_request(self) -> None:
        """Validate a browser-blocked live playlist without weakening Chromium."""
        if not self._blocked_requests:
            return
        url, headers, generation, line = self._blocked_requests.pop(0)
        if generation != self._generation or self._pending_line:
            return
        kind = "hls" if urlsplit(url).path.lower().endswith(".m3u8") else "flv"
        segments: set[str] = set()
        if kind == "hls":
            try:
                for _ in range(4):
                    response = self._context.request.get(url, headers=headers, timeout=5_000)
                    try:
                        if not 200 <= response.status < 300:
                            raise ValueError(tr("Playlist unavailable"))
                        body = response.body().decode("utf-8", errors="replace")
                        url = response.url
                    finally:
                        response.dispose()
                    if not _live_playlist(body):
                        raise ValueError(tr("Not a live playlist"))
                    child = _first_variant(url, body)
                    if child:
                        url = child
                        continue
                    segments = _playlist_segments(url, body)
                    break
                if not segments:
                    raise ValueError(tr("The live playlist has no segments"))
            except Exception:
                self.message = tr(
                    "The browser blocked the media request and playlist validation failed; choose another stream"
                )
                return
        with self._lock:
            if generation != self._generation or self._pending_line:
                return
            now = time.monotonic()
            self._candidates[url] = Candidate(
                url,
                kind,
                headers,
                now,
                now,
                line_text=line,
                identifier=self._next_identifier,
                segments=segments,
                browser_blocked=True,
                segment_duration=_playlist_segment_duration(body) if kind == "hls" else 2.0,
            )
            self._next_identifier += 1
            self.message = tr("Captured the blocked media request; waiting for ffprobe validation")

    def _last_click_text(self) -> str | None:
        if self._page is None:
            return None
        try:
            value = self._page.evaluate("window.__footboy_last_click || null")
            return value if isinstance(value, str) and is_line_label(value) else None
        except Exception:
            return None

    def _selection_loop(self) -> Source:
        assert self._page is not None and self._context is not None
        last_print = 0.0
        last_discovery = 0.0
        while time.monotonic() - self._started < self.timeout:
            if self._abort.is_set():
                raise SniffError(tr("Stream discovery cancelled"))
            self._page.wait_for_timeout(250)
            now = time.monotonic()
            try:
                while True:
                    self._pending_line = self._line_commands.get_nowait()
                    last_discovery = 0.0
            except queue.Empty:
                pass
            if now - last_discovery >= 1.0 and hasattr(self._page, "frames"):
                self._discover_lines()
                if self._pending_line:
                    self._reuse_line(self._pending_line)
                last_discovery = now
            self._read_blocked_request()
            self._update_stability(now)
            ranked = self._ranked(now)
            if now - last_print >= 1.0:
                self._print_candidates(ranked, now)
                last_print = now

            command = self._next_command()
            if command == "c":
                for candidate in ranked[:1]:
                    candidate.cancelled_until = now + 30
                    candidate.stable_since = None
                print(tr("Automatic selection cancelled; continuing stream discovery."))
            chosen = None
            if command in {"", "enter"} and ranked:
                chosen = ranked[0]
            elif command.isdigit():
                chosen = next((c for c in ranked if c.identifier == int(command)), None)
            if chosen is None and ranked and command != "c" and self._auto_select:
                best = ranked[0]
                if best.ready_for_auto_select(now):
                    chosen = best
            if chosen is not None:
                try:
                    self.message = tr("Validating the selected stream with ffprobe")
                    return self._confirm(chosen)
                except MediaProbeError as exc:
                    chosen.cancelled_until = time.monotonic() + 30
                    chosen.stable_since = None
                    self.message = tr("Stream validation failed; choose another stream: {0}", exc)
                    print(self.message, file=sys.stderr)
        raise SniffError(
            tr(
                "No usable live stream confirmed within {0:g} seconds; {1}",
                self.timeout,
                self.message,
            )
        )

    def _update_stability(self, now: float) -> None:
        with self._lock:
            for candidate in self._candidates.values():
                if (
                    candidate.active(now) or candidate.browser_blocked
                ) and now >= candidate.cancelled_until:
                    if candidate.stable_since is None:
                        candidate.stable_since = now
                else:
                    candidate.stable_since = None

    def _ranked(self, now: float) -> list[Candidate]:
        with self._lock:
            if self._pending_line:
                return []
            return sorted(
                self._candidates.values(),
                key=lambda item: (
                    now >= item.cancelled_until,
                    item.active(now),
                    item.last_segment_at,
                    item.last_seen,
                ),
                reverse=True,
            )

    @staticmethod
    def _print_candidates(candidates: list[Candidate], now: float) -> None:
        if not candidates:
            print(
                tr("Waiting for playback requests… (select a match and stream in the browser)"),
                end="\r",
                flush=True,
            )
            return
        print(tr("\nCandidate live streams:"))
        for candidate in candidates:
            active = candidate.active(now)
            stable = now - candidate.stable_since if candidate.stable_since is not None else 0
            if active and stable >= 5:
                status = tr("Playing; auto-confirming in {0:.0f}s", max(0, 10 - stable))
            elif active:
                status = tr("Playing; checking stability {0:.0f}/5s", stable)
            elif candidate.browser_blocked:
                status = tr("Blocked by the browser; waiting for media validation")
            else:
                status = tr("No recent segments found")
            print(
                tr(
                    "  [{0}] {1}  {2}  {3}",
                    candidate.identifier,
                    candidate.line_text or tr("Unnamed stream"),
                    status,
                    candidate.display_url,
                )
            )
        print(
            tr(
                "Press Enter to confirm the first stream, enter its number to choose another, or c to cancel automatic selection."
            ),
            flush=True,
        )

    def _confirm(self, candidate: Candidate) -> Source:
        if self._abort.is_set():
            raise SniffError(tr("Stream discovery cancelled"))
        generation = self._generation
        cookies = self._context.cookies([candidate.url, *candidate.segments])
        source = Source(
            url=candidate.url,
            headers=dict(candidate.headers),
            cookies=cookies,
            kind=candidate.kind,
            line_text=candidate.line_text,
            no_proxy=self.no_proxy,
        )
        probed = ffprobe_source(source, ffprobe=self.ffprobe, timeout=15)
        if self._abort.is_set():
            raise SniffError(tr("Stream discovery cancelled"))
        if generation != self._generation or self._pending_line:
            raise MediaProbeError(tr("Stream changed; ignoring the previous stream's probe result"))
        print(
            tr(
                "Stream confirmed: codec={0}, audio={1}",
                probed.video_codec,
                "yes" if probed.has_audio else "no",
            )
        )
        return probed

    def _keyboard_loop(self) -> None:
        if not sys.stdin or not sys.stdin.isatty():
            return
        if os.name == "nt":
            import msvcrt

            buffer = ""
            while not self._stop_keyboard.wait(0.05):
                if not msvcrt.kbhit():
                    continue
                char = msvcrt.getwch()
                if char == "\r":
                    self._commands.put(buffer.strip().lower() or "enter")
                    buffer = ""
                else:
                    buffer += char
            return
        while not self._stop_keyboard.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.2)
            if readable:
                value = sys.stdin.readline()
                if value == "":
                    return
                self._commands.put(value.strip().lower() or "enter")

    def _next_command(self) -> str:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return "none"


def _playlist_segments(url: str, body: str) -> set[str]:
    result = set()
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            result.add(urljoin(url, line))
        elif line.startswith("#EXT-X-PART:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                result.add(urljoin(url, match.group(1)))
    return result


def _playlist_segment_duration(body: str) -> float:
    durations = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith(("#EXT-X-TARGETDURATION:", "#EXTINF:")):
            continue
        try:
            value = float(line.partition(":")[2].partition(",")[0])
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            durations.append(value)
    return max(durations, default=2.0)


def _live_playlist(body: str) -> bool:
    return (
        body.lstrip().startswith("#EXTM3U")
        and "#EXT-X-ENDLIST" not in body
        and not re.search(r"#EXT-X-PLAYLIST-TYPE:\s*VOD", body)
    )


def _first_variant(url: str, body: str) -> str | None:
    variant = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            variant = True
        elif variant and line and not line.startswith("#"):
            return urljoin(url, line)
    return None


def _request_headers(request: Any) -> dict[str, str]:
    try:
        headers = request.all_headers()
    except Exception:
        headers = request.headers
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in {"user-agent", "referer", "origin"}
    }
