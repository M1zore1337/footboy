from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from footboy.sources.lines import validate_line_text

MIME_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}
FILE_CHUNK_SIZE = 64 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


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
        self.access_token = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self.access_token = secrets.token_urlsafe(32)
        handler = _handler_factory(self.controller, self.hls_dir, access_token=self.access_token)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="footboy-http", daemon=True
        )
        self._thread.start()
        addresses = ["127.0.0.1", *_lan_ipv4_addresses()] if self.host == "0.0.0.0" else [self.host]
        print(f"本次控制密钥：{self.access_token}")
        print("控制页（仅将含密钥的链接交给可信操作者）：")
        for address in addresses:
            print(f"  http://{address}:{self.port}/#token={self.access_token}")
        print("HLS 播放地址（无需控制密钥）：")
        for address in addresses:
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


def _handler_factory(
    controller: Controller, hls_dir: Path, *, access_token: str
) -> type[BaseHTTPRequestHandler]:
    if not access_token:
        raise ValueError("控制密钥不能为空")
    static_dir = Path(__file__).with_name("static").resolve()
    index_path = static_dir / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "Footboy/0.1"
        _media_cors = False

        def log_message(self, format: str, *args: object) -> None:
            return

        def end_headers(self) -> None:
            if self._media_cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Range")
                self.send_header(
                    "Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges"
                )
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            super().end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                if not self._authorize_api():
                    return
            elif self._hls_candidate(path) is not None:
                self._media_cors = True
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith("/api/") and not self._authorize_api():
                return
            if path == "/":
                self._send_file(index_path, "text/html; charset=utf-8", no_store=True)
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
            diagnostic = re.fullmatch(r"/api/ocr/(video|bili)/(crop|processed)\.png", path)
            if diagnostic:
                label, kind = diagnostic.groups()
                version = parse_qs(urlsplit(self.path).query).get("v", [None])[0]
                preview = getattr(controller, "ocr_image", lambda *_: None)(label, kind, version)
                if preview is None:
                    self._send_json({"error": "本次识别图像暂不可用"}, status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_bytes(preview, "image/png", no_store=True)
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
                self._send_file(candidate, mime, no_store=False, revalidate=True)
                return
            self._serve_hls(path)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not self._authorize_api():
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json(
                    {"ok": False, "error": "请求必须使用 application/json"},
                    status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return
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
                elif path == "/api/audio":
                    self._controller_action("request_audio", body)
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

        def _authorize_api(self) -> bool:
            origins = self.headers.get_all("Origin", [])
            hosts = self.headers.get_all("Host", [])
            if (
                len(origins) > 1
                or (origins and (len(hosts) != 1 or not _same_origin(origins[0], hosts[0])))
                or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}
            ):
                self._send_json(
                    {"ok": False, "error": "控制接口仅接受同源浏览器请求"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return False
            authorization = self.headers.get_all("Authorization", [])
            supplied = authorization[0] if len(authorization) == 1 else ""
            scheme, _, token = supplied.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                token.lstrip(" ").encode("utf-8"), access_token.encode("ascii")
            ):
                self._send_json(
                    {"ok": False, "error": "请使用本次启动的控制密钥"},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return False
            return True

        def _controller_action(self, name: str, *args: Any) -> None:
            method = getattr(controller, name, None)
            if method is None:
                raise NotImplementedError("此运行模式不支持该操作")
            method(*args)

        def _hls_candidate(self, request_path: str) -> Path | None:
            relative = unquote(request_path).lstrip("/")
            if not relative or "/" in relative or "\\" in relative:
                return None
            candidate = (hls_dir / relative).resolve()
            if candidate.parent != hls_dir or candidate.suffix.lower() not in MIME_TYPES:
                return None
            return candidate

        def _serve_hls(self, request_path: str) -> None:
            candidate = self._hls_candidate(request_path)
            if candidate is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._media_cors = True
            self._send_file(
                candidate,
                MIME_TYPES[candidate.suffix.lower()],
                no_store=candidate.suffix.lower() == ".m3u8",
                allow_range=candidate.suffix.lower() != ".m3u8",
            )

        def _send_file(
            self,
            candidate: Path,
            content_type: str,
            *,
            no_store: bool,
            allow_range: bool = False,
            revalidate: bool = False,
        ) -> None:
            try:
                handle = candidate.open("rb")
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with handle:
                stat = os.fstat(handle.fileno())
                length = stat.st_size
                etag = f'W/"{stat.st_mtime_ns:x}-{length:x}"'
                validators = self.headers.get("If-None-Match", "").split(",")
                if revalidate and any(value.strip() in {etag, "*"} for value in validators):
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                requested = self.headers.get("Range") if allow_range else None
                try:
                    start, end = _byte_range(requested, length)
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{length}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.PARTIAL_CONTENT if requested else HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(end - start + 1))
                if allow_range:
                    self.send_header("Accept-Ranges", "bytes")
                if requested:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{length}")
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                elif revalidate:
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("ETag", etag)
                self.end_headers()
                if self.command == "HEAD":
                    return
                handle.seek(start)
                remaining = end - start + 1
                try:
                    while remaining:
                        chunk = handle.read(min(FILE_CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # A player may discard a segment when changing streams.

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
            if status == HTTPStatus.UNAUTHORIZED:
                self.send_header("WWW-Authenticate", 'Bearer realm="Footboy"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            *,
            no_store: bool,
        ) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    return Handler


def _same_origin(origin: str, host: str) -> bool:
    try:
        actual, expected = urlsplit(origin), urlsplit(f"http://{host}")
        actual_port = actual.port if actual.port is not None else 80
        expected_port = expected.port if expected.port is not None else 80
        return (
            actual.scheme == "http"
            and bool(actual.hostname)
            and not (
                actual.username or actual.password or actual.path or actual.query or actual.fragment
            )
            and (actual.hostname, actual_port) == (expected.hostname, expected_port)
        )
    except ValueError:
        return False


def _byte_range(requested: str | None, length: int) -> tuple[int, int]:
    if requested is None:
        return 0, length - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
    if not match or not (match[1] or match[2]) or length == 0:
        raise ValueError("无效字节范围")
    if match[1]:
        start = int(match[1])
        end = min(int(match[2]), length - 1) if match[2] else length - 1
    else:
        suffix = int(match[2])
        if suffix == 0:
            raise ValueError("无效字节范围")
        start, end = max(0, length - suffix), length - 1
    if start > end or start >= length:
        raise ValueError("无效字节范围")
    return start, end


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
