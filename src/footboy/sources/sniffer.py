from __future__ import annotations

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

    def active(self, now: float) -> bool:
        # A continuously downloaded FLV has no child segment requests; its own
        # response stays open, so there will not be periodic response events.
        if self.kind == "flv":
            return self.request_open
        last_activity = self.last_segment_at
        return last_activity > 0 and now - last_activity <= 3.0

    @property
    def display_url(self) -> str:
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class StreamSniffer:
    def __init__(self, *, ffprobe: str | Path = "ffprobe", timeout: float = 300) -> None:
        self.ffprobe = ffprobe
        self.timeout = timeout
        self._candidates: dict[str, Candidate] = {}
        self._lock = threading.RLock()
        self._commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._stop_keyboard = threading.Event()
        self._abort = threading.Event()
        self._page: Any = None
        self._context: Any = None
        self._recent_requests: dict[str, float] = {}
        self._started = 0.0
        self.running = False
        self.message = ""

    def sniff(
        self, page_url: str, *, preferred_line_text: str | None = None, headless: bool = False
    ) -> Source:
        if self._abort.is_set():
            raise SniffError("嗅探已取消")
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SniffError(
                "未安装 Playwright；请先安装依赖并运行 playwright install chromium"
            ) from exc

        self._candidates.clear()
        self._recent_requests.clear()
        self._commands = queue.SimpleQueue()
        self._started = time.monotonic()
        self.running = True
        self._stop_keyboard.clear()
        keyboard = threading.Thread(
            target=self._keyboard_loop, name="sniffer-keyboard", daemon=True
        )
        keyboard.start()
        try:
            with sync_playwright() as playwright:
                browser = self._launch_browser(playwright, PlaywrightError, headless=headless)
                try:
                    self._context = browser.new_context()
                    self._page = self._context.new_page()
                    main_page = self._page
                    self._context.on("page", lambda page: self._close_popup(page, main_page))
                    self._context.on("response", self._on_response)
                    self._context.on("request", self._on_request)
                    self._context.on("requestfinished", self._on_request_finished)
                    self._context.on("requestfailed", self._on_request_finished)
                    self._page.add_init_script(
                        """
                        document.addEventListener('click', event => {
                          const el = event.target && event.target.closest('button,a,[role=button]');
                          if (el) window.__footboy_last_click = (el.innerText || el.textContent || '').trim();
                        }, true);
                        """
                    )
                    try:
                        self._page.goto(page_url, wait_until="domcontentloaded", timeout=15_000)
                    except PlaywrightError as exc:
                        print(f"页面加载未完全结束，将继续嗅探: {exc}", file=sys.stderr)
                    if preferred_line_text:
                        self._reuse_line(preferred_line_text)
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
            raise ValueError("当前没有正在进行的嗅探")
        self._commands.put(str(identifier) if identifier is not None else "c")

    def public_status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {
                "running": self.running,
                "message": self.message,
                "candidates": [
                    {
                        "id": item.identifier,
                        "url": item.display_url,
                        "kind": item.kind,
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
        playwright: Any, error_type: type[Exception], *, headless: bool = False
    ) -> Any:
        args = ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]
        failures = []
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = {"headless": headless, "args": args}
                if channel:
                    kwargs["channel"] = channel
                return playwright.chromium.launch(**kwargs)
            except error_type as exc:
                failures.append(f"{channel or 'chromium'}: {exc}")
        raise SniffError("Chrome、Edge 和 Playwright Chromium 均无法启动：" + "；".join(failures))

    @staticmethod
    def _close_popup(page: Any, main_page: Any) -> None:
        if page is not main_page:
            try:
                page.close()
            except Exception:
                pass

    def _reuse_line(self, text: str) -> None:
        assert self._page is not None
        try:
            locator = self._page.get_by_text(text, exact=True)
            if locator.count():
                locator.first.click(timeout=5_000)
                print(f"已尝试复用上次线路: {text}")
        except Exception as exc:
            print(f"未能自动点击上次线路“{text}”: {exc}", file=sys.stderr)

    def _on_response(self, response: Any) -> None:
        if response.status < 200 or response.status >= 300:
            return
        now = time.monotonic()
        url = response.url
        content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].lower()
        lower_url = urlsplit(url).path.lower()

        kind: str | None = None
        segments: set[str] = set()
        if ".m3u8" in lower_url or content_type in PLAYLIST_TYPES:
            try:
                body = response.body().decode("utf-8", errors="replace")
            except Exception:
                return
            if "#EXTM3U" not in body or "#EXT-X-ENDLIST" in body:
                return
            # A master manifest is not itself playing. Only media playlists
            # with exact child requests can become an active candidate.
            if "#EXT-X-STREAM-INF" in body or "#EXT-X-I-FRAME-STREAM-INF" in body:
                return
            kind = "hls"
            segments = _playlist_segments(url, body)
        elif ".flv" in lower_url or content_type in FLV_TYPES:
            kind = "flv"

        if kind:
            try:
                all_headers = response.request.all_headers()
            except Exception:
                all_headers = response.request.headers
            headers = {
                name: value
                for name, value in all_headers.items()
                if name.lower() in {"user-agent", "referer", "origin"}
            }
            with self._lock:
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
                        line_text=self._last_click_text(),
                        identifier=len(self._candidates) + 1,
                    )
                    self._candidates[url] = candidate
                candidate.segments = segments
                candidate.request_open = kind == "flv"
                candidate.last_segment_at = max(
                    (self._recent_requests.get(segment, 0) for segment in segments), default=0
                )

    def _on_request(self, request: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self._recent_requests[request.url] = now
            if len(self._recent_requests) > 500:
                self._recent_requests = {
                    url: when for url, when in self._recent_requests.items() if now - when < 20
                }
            for candidate in self._candidates.values():
                if request.url in candidate.segments:
                    candidate.last_segment_at = now

    def _on_request_finished(self, request: Any) -> None:
        with self._lock:
            candidate = self._candidates.get(request.url)
            if candidate and candidate.kind == "flv":
                candidate.request_open = False

    def _last_click_text(self) -> str | None:
        if self._page is None:
            return None
        try:
            value = self._page.evaluate("window.__footboy_last_click || null")
            return str(value)[:200] if value else None
        except Exception:
            return None

    def _selection_loop(self) -> Source:
        assert self._page is not None and self._context is not None
        last_print = 0.0
        while time.monotonic() - self._started < self.timeout:
            if self._abort.is_set():
                raise SniffError("嗅探已取消")
            self._page.wait_for_timeout(250)
            now = time.monotonic()
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
                print("已取消当前自动选择，继续嗅探。")
            chosen = None
            if command in {"", "enter"} and ranked:
                chosen = ranked[0]
            elif command.isdigit():
                chosen = next((c for c in ranked if c.identifier == int(command)), None)
            if chosen is None and ranked and command != "c":
                best = ranked[0]
                if best.stable_since is not None and now - best.stable_since >= 10:
                    chosen = best
            if chosen is not None:
                try:
                    self.message = "正在用 ffprobe 验收所选线路"
                    return self._confirm(chosen)
                except MediaProbeError as exc:
                    chosen.cancelled_until = time.monotonic() + 30
                    chosen.stable_since = None
                    self.message = f"线路探测失败，请换线路: {exc}"
                    print(self.message, file=sys.stderr)
        raise SniffError("5 分钟内未确认可用直播线路")

    def _update_stability(self, now: float) -> None:
        with self._lock:
            for candidate in self._candidates.values():
                if candidate.active(now) and now >= candidate.cancelled_until:
                    if candidate.stable_since is None:
                        candidate.stable_since = now
                else:
                    candidate.stable_since = None

    def _ranked(self, now: float) -> list[Candidate]:
        with self._lock:
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
            print("等待播放请求……（请在浏览器中选择比赛和线路）", end="\r", flush=True)
            return
        print("\n候选直播流：")
        for candidate in candidates:
            active = candidate.active(now)
            stable = now - candidate.stable_since if candidate.stable_since is not None else 0
            if active and stable >= 5:
                status = f"正在播放，{max(0, 10 - stable):.0f}s 后自动确认"
            elif active:
                status = f"正在播放，稳定确认 {stable:.0f}/5s"
            else:
                status = "未发现近期分片"
            print(f"  [{candidate.identifier}] {status}  {candidate.display_url}")
        print("回车确认首项，输入编号确认指定项，输入 c 取消当前自动选择。", flush=True)

    def _confirm(self, candidate: Candidate) -> Source:
        cookies = self._context.cookies([candidate.url, *candidate.segments])
        source = Source(
            url=candidate.url,
            headers=dict(candidate.headers),
            cookies=cookies,
            kind=candidate.kind,
            line_text=candidate.line_text,
        )
        probed = ffprobe_source(source, ffprobe=self.ffprobe, timeout=15)
        print(
            f"已确认线路: codec={probed.video_codec}, audio={'yes' if probed.has_audio else 'no'}"
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
