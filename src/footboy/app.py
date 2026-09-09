from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from footboy.environment import check_binary
from footboy.sources.bili import BiliResolveError, _room_id
from footboy.sources.lines import validate_line_text
from footboy.supervisor import Supervisor, SupervisorConfig


class SessionConflict(RuntimeError):
    pass


class Application:
    """Keep the web console alive before, between, and after live sessions."""

    def __init__(self, defaults: SupervisorConfig) -> None:
        self.defaults = defaults
        self.session: Supervisor | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._session_id = 0

    def request_start(self, body: dict[str, Any]) -> None:
        config = session_config(self.defaults, body)
        with self._lock:
            if self._closed:
                raise SessionConflict("控制台正在关闭")
            if self._thread is not None and self._thread.is_alive():
                raise SessionConflict("已有任务正在运行或停止，请等待它结束后再连接")
            session = Supervisor(config)
            session._set_phase("CHECK", "正在检查 FFmpeg 和 ffprobe")
            self.session = session
            self._session_id = time.time_ns() // 1_000_000
            self._thread = threading.Thread(
                target=self._run_session, args=(session,), name="footboy-session", daemon=True
            )
            self._thread.start()

    @staticmethod
    def _run_session(session: Supervisor) -> None:
        try:
            check_binary(session.config.ffmpeg, minimum_major=6)
            check_binary(session.config.ffprobe)
        except RuntimeError as exc:
            session._set_phase("ERROR", f"环境检查失败：{exc}")
            return
        if not session._stop.is_set():
            session.run(serve=False)
        else:
            session._set_phase("STOPPED", "任务已停止")

    def request_stop(self) -> None:
        with self._lock:
            if self.session is not None:
                self.session.stop()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=45)

    def _active_session(self) -> Supervisor:
        with self._lock:
            if self.session is None or self._thread is None or not self._thread.is_alive():
                raise SessionConflict("请先连接比赛画面和直播间")
            if self.session.phase == "STOPPING":
                raise SessionConflict("任务正在停止")
            return self.session

    def request_offset_delta(self, delta_ms: int) -> None:
        self._active_session().request_offset_delta(delta_ms)

    def request_remeasure(self) -> None:
        self._active_session().request_remeasure()

    def request_audio(self, body: dict[str, Any]) -> None:
        self._active_session().request_audio(body)

    def request_resniff(self) -> None:
        self._active_session().request_resniff()

    def request_select_source(self, identifier: int | None) -> None:
        self._active_session().request_select_source(identifier)

    def request_switch_line(self, text: str) -> None:
        self._active_session().request_switch_line(text)

    def request_roi(self, label: str, value: dict[str, Any]) -> None:
        self._active_session().request_roi(label, value)

    def preview(self, label: str) -> bytes | None:
        with self._lock:
            return self.session.preview(label) if self.session else None

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            value = (
                self.session.public_status()
                if self.session is not None
                else {
                    "state": "IDLE",
                    "aligned": False,
                    "offset_seconds": 0.0,
                    "applied_offset_seconds": None,
                    "confidence": None,
                    "last_verified_at": None,
                    "ffmpeg": {"running": False},
                    "message": "输入比赛页面和 B 站直播间，开始连接",
                    "estimated_latency_seconds": "较慢源自身延迟 + HLS 缓冲约 6–10",
                    "measurement": {"running": False, "needs_roi": [], "sources": {}},
                }
            )
            value["active"] = self._thread is not None and self._thread.is_alive()
            value["session_id"] = self._session_id
            value["capabilities"] = {
                "session_controls": True,
                "roi": True,
                "select_source": True,
                "switch_line": True,
                "audio_mix": True,
            }
            value["defaults"] = {
                "auto_measure": self.defaults.auto_measure,
                "offset_seconds": self.defaults.initial_offset,
                "video_direct": self.defaults.video_direct,
                "video_no_proxy": self.defaults.video_no_proxy,
                "video_line_text": self.defaults.video_line_text,
                "bili_direct": self.defaults.bili_direct,
            }
            return value


def session_config(defaults: SupervisorConfig, body: dict[str, Any]) -> SupervisorConfig:
    video = _http_url(body.get("video_url"), "比赛地址")
    bili = _http_url(body.get("bili_url"), "B站地址")
    video_direct = _boolean(body, "video_direct", defaults.video_direct)
    bili_direct = _boolean(body, "bili_direct", defaults.bili_direct)
    if not bili_direct:
        try:
            _room_id(bili)
        except BiliResolveError as exc:
            raise ValueError(str(exc)) from exc
    auto = _boolean(body, "auto_measure", defaults.auto_measure)
    offset = body.get("offset_seconds", defaults.initial_offset)
    if offset is not None:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, (float, int))
            or not math.isfinite(offset)
        ):
            raise ValueError("初始偏移必须是有限数值")
        offset = float(offset)
    return replace(
        defaults,
        video_page_url=video,
        bili_room_url=bili,
        video_direct=video_direct,
        video_no_proxy=_boolean(body, "video_no_proxy", defaults.video_no_proxy),
        video_line_text=validate_line_text(
            body.get("video_line_text", defaults.video_line_text), optional=True
        )
        if not video_direct
        else None,
        bili_direct=bili_direct,
        video_headers=_headers(body.get("video_headers", defaults.video_headers)),
        bili_headers=_headers(body.get("bili_headers", defaults.bili_headers)),
        auto_measure=auto,
        initial_offset=offset,
    )


def _http_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError(f"{label}必须是 HTTP(S) URL")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{label}必须是 HTTP(S) URL")
    if parts.username or parts.password or any(char.isspace() for char in value):
        raise ValueError(f"{label}不能含内嵌账号密码或空白字符")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"{label}端口无效") from exc
    return value


def _boolean(body: dict[str, Any], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 40:
        raise ValueError("请求头必须是 JSON 对象")
    result = {}
    for name, content in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("请求头名称无效")
        if not isinstance(content, str) or any(c in content for c in "\r\n\0"):
            raise ValueError("请求头值无效")
        result[name] = content
    return result
