from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from footboy.sources.lines import validate_line_text

MIME_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}


class Controller(Protocol):
    def public_status(self) -> dict[str, Any]: ...
    def request_offset_delta(self, delta_ms: int) -> None: ...
    def request_remeasure(self) -> None: ...
    def request_resniff(self) -> None: ...


class ControlServer:
    def __init__(
        self,
        controller: Controller,
        hls_dir: str | Path,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.controller = controller
        self.hls_dir = Path(hls_dir).resolve()
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        handler = _handler_factory(self.controller, self.hls_dir)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="footboy-http", daemon=True
        )
        self._thread.start()
        addresses = _lan_ipv4_addresses() if self.host == "0.0.0.0" else [self.host]
        if not addresses:
            addresses = ["127.0.0.1"]
        print("控制页与 HLS 地址：")
        for address in addresses:
            print(f"  http://{address}:{self.port}/")
            print(f"  http://{address}:{self.port}/live.m3u8")
        if os.name == "nt":
            print("若其他设备无法访问，请在 Windows 防火墙首次提示中允许专用网络访问。")

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None


def _handler_factory(controller: Controller, hls_dir: Path) -> type[BaseHTTPRequestHandler]:
    static_dir = Path(__file__).with_name("static").resolve()
    index_path = static_dir / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "Footboy/0.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
            self.send_header(
                "Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges"
            )
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(index_path.read_bytes(), "text/html; charset=utf-8", no_store=True)
                return
            if path == "/api/status":
                self._send_json(controller.public_status())
                return
            if path in {"/api/snapshot/video.jpg", "/api/snapshot/bili.jpg"}:
                label = path.rsplit("/", 1)[-1][:-4]
                preview = getattr(controller, "preview", lambda _: None)(label)
                if preview is None:
                    self._send_json({"error": "尚无采样画面"}, status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_bytes(preview, "image/jpeg", no_store=True)
                return
            if path.startswith("/static/"):
                candidate = (static_dir / unquote(path[len("/static/") :])).resolve()
                if not candidate.is_relative_to(static_dir) or not candidate.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                mime = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }.get(candidate.suffix, mimetypes.guess_type(candidate.name)[0] or "text/plain")
                self._send_bytes(candidate.read_bytes(), mime, no_store=False)
                return
            self._serve_hls(path)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            try:
                body = self._read_json()
                if path == "/api/offset":
                    delta = body.get("delta_ms")
                    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                        raise ValueError("delta_ms 必须是数字")
                    if not math.isfinite(delta) or delta != int(delta):
                        raise ValueError("delta_ms 必须是有限整数")
                    delta_int = int(delta)
                    if abs(delta_int) > 300_000:
                        raise ValueError("单次调整不得超过 300 秒")
                    controller.request_offset_delta(delta_int)
                elif path == "/api/remeasure":
                    controller.request_remeasure()
                elif path == "/api/resniff":
                    controller.request_resniff()
                elif path == "/api/start":
                    self._controller_action("request_start", body)
                elif path == "/api/stop":
                    self._controller_action("request_stop")
                elif path == "/api/roi":
                    self._controller_action("request_roi", body.get("source"), body)
                elif path == "/api/source":
                    identifier = body.get("id")
                    if identifier is not None and (
                        isinstance(identifier, bool)
                        or not isinstance(identifier, int)
                        or identifier < 1
                    ):
                        raise ValueError("线路 id 必须是正整数；null 取消自动选择")
                    self._controller_action("request_select_source", identifier)
                elif path == "/api/line":
                    self._controller_action(
                        "request_switch_line", validate_line_text(body.get("text"))
                    )
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            except (ValueError, UnicodeError, KeyError, TypeError, OverflowError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except NotImplementedError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)}, status=HTTPStatus.METHOD_NOT_ALLOWED
                )
                return
            except RuntimeError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True}, status=HTTPStatus.ACCEPTED)

        def _controller_action(self, name: str, *args: Any) -> None:
            method = getattr(controller, name, None)
            if method is None:
                raise NotImplementedError("此运行模式不支持该操作")
            method(*args)

        def _serve_hls(self, request_path: str, *, head_only: bool = False) -> None:
            relative = unquote(request_path).lstrip("/")
            if not relative or "/" in relative or "\\" in relative:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = (hls_dir / relative).resolve()
            if candidate.parent != hls_dir or candidate.suffix.lower() not in MIME_TYPES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                content = candidate.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = (
                MIME_TYPES.get(candidate.suffix.lower()) or mimetypes.guess_type(candidate.name)[0]
            )
            self._send_bytes(
                content,
                content_type or "application/octet-stream",
                no_store=candidate.suffix == ".m3u8",
                head_only=head_only,
                allow_range=candidate.suffix != ".m3u8",
            )

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length 无效") from exc
            if length < 0 or length > 64 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"

            def invalid_constant(value: str) -> None:
                raise ValueError(f"JSON 不能包含 {value}")

            value = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
            if not isinstance(value, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return value

        def _send_json(self, value: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            *,
            no_store: bool,
            head_only: bool = False,
            allow_range: bool = False,
        ) -> None:
            length = len(payload)
            start, end = 0, length - 1
            range_header = self.headers.get("Range") if allow_range else None
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if match and (match[1] or match[2]):
                    if match[1]:
                        start = int(match[1])
                        end = min(int(match[2]), length - 1) if match[2] else length - 1
                    else:
                        start = max(0, length - int(match[2]))
                if not match or not (match[1] or match[2]) or start > end or start >= length:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{length}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = payload[start : end + 1]
            self.send_response(HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if allow_range:
                self.send_header("Accept-Ranges", "bytes")
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{length}")
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only and self.command != "HEAD":
                self.wfile.write(payload)

    return Handler


def _lan_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    # Hostname lookup can be incomplete on Windows/macOS. A UDP connect does
    # not transmit data but asks the OS which local interface it would use.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
        if not address.startswith("127."):
            addresses.add(address)
    except OSError:
        pass
    finally:
        sock.close()
    return sorted(addresses)
