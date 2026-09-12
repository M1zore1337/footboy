from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from footboy.serve.http import FILE_CHUNK_SIZE, ControlServer


class FakeController:
    def __init__(self) -> None:
        self.deltas: list[int] = []
        self.remeasure = 0
        self.resniff = 0
        self.starts = []
        self.stops = 0
        self.rois = []
        self.selections = []
        self.lines = []
        self.audio = []

    def public_status(self):
        return {"state": "RUN", "offset_seconds": -1.5, "aligned": True}

    def request_offset_delta(self, delta_ms: int) -> None:
        self.deltas.append(delta_ms)

    def request_remeasure(self) -> None:
        self.remeasure += 1

    def request_audio(self, body):
        self.audio.append(body)

    def request_resniff(self) -> None:
        self.resniff += 1

    def request_start(self, body):
        self.starts.append(body)

    def request_stop(self):
        self.stops += 1

    def request_roi(self, label, value):
        self.rois.append((label, value))

    def request_select_source(self, identifier):
        self.selections.append(identifier)

    def request_switch_line(self, text):
        self.lines.append(text)

    def preview(self, label):
        return b"jpeg-fixture" if label == "video" else None


@pytest.fixture
def running_server(tmp_path):
    controller = FakeController()
    (tmp_path / "live.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (tmp_path / "seg_000001.ts").write_bytes(b"segment")
    server = ControlServer(controller, tmp_path, host="127.0.0.1", port=0)
    server.start()
    try:
        yield controller, f"http://127.0.0.1:{server.port}", server
    finally:
        server.stop()


def api_request(running_server, path, *, data=None, headers=None, method=None):
    _, base, server = running_server
    return urllib.request.Request(
        base + path,
        data=data,
        headers={
            "Authorization": f"Bearer {server.access_token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method=method,
    )


def test_status_is_private_and_hls_remains_public(running_server) -> None:
    _, base, _ = running_server
    with urllib.request.urlopen(api_request(running_server, "/api/status")) as response:
        assert json.load(response)["state"] == "RUN"
        assert "Access-Control-Allow-Origin" not in response.headers
    with urllib.request.urlopen(base + "/live.m3u8") as response:
        assert response.headers.get_content_type() == "application/vnd.apple.mpegurl"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Access-Control-Allow-Origin"] == "*"
    head = urllib.request.Request(base + "/seg_000001.ts", method="HEAD")
    with urllib.request.urlopen(head) as response:
        assert response.headers.get_content_type() == "video/mp2t"
        assert response.headers["Content-Length"] == "7"


def test_control_actions_and_validation(running_server) -> None:
    controller, _, _ = running_server
    request = api_request(
        running_server,
        "/api/offset",
        data=b'{"delta_ms":-500}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        assert response.status == 202
    assert controller.deltas == [-500]

    bad = api_request(
        running_server,
        "/api/offset",
        data=b'{"delta_ms":"oops"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(bad)
    assert error.value.code == 400


def test_audio_control_accepts_independent_partial_updates(running_server):
    controller, _, _ = running_server
    for body in ({"original_volume": 0.3}, {"commentary_volume": 0}):
        request = api_request(
            running_server,
            "/api/audio",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
    assert controller.audio == [{"original_volume": 0.3}, {"commentary_volume": 0}]


def test_path_traversal_is_not_served(running_server) -> None:
    _, base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(base + "/..%2Fstate.json")
    assert error.value.code == 404


@pytest.mark.parametrize(
    "body",
    [
        b'{"delta_ms":NaN}',
        b'{"delta_ms":Infinity}',
        b'{"delta_ms":1e999}',
        b'{"delta_ms":true}',
        b'{"delta_ms":300001}',
        b'{"delta_ms":0.5}',
        b"[]",
        b"\xff",
    ],
)
def test_invalid_offset_requests_return_json_400(running_server, body):
    request = api_request(running_server, "/api/offset", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400
    assert json.load(error.value)["ok"] is False


def test_media_range_requests_and_invalid_range(running_server):
    _, base, _ = running_server
    for requested, expected, content_range in (
        ("bytes=1-3", b"egm", "bytes 1-3/7"),
        ("bytes=-3", b"ent", "bytes 4-6/7"),
        ("bytes=4-", b"ent", "bytes 4-6/7"),
    ):
        request = urllib.request.Request(base + "/seg_000001.ts", headers={"Range": requested})
        with urllib.request.urlopen(request) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == content_range
            assert response.read() == expected
    request = urllib.request.Request(base + "/seg_000001.ts", headers={"Range": "bytes=9-10"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 416


def test_web_actions_and_snapshot(running_server):
    controller, _, _ = running_server
    for route, value in (
        ("start", {"video_url": "https://example/a", "bili_url": "https://live.bilibili.com/0"}),
        ("stop", {}),
        ("roi", {"source": "video", "roi": [0, 0, 0.2, 0.2], "flip": "h", "inverted": False}),
        ("source", {"id": 2}),
        ("source", {"id": None}),
        ("line", {"text": "高清直播5"}),
    ):
        request = api_request(
            running_server, "/api/" + route, data=json.dumps(value).encode(), method="POST"
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
    assert controller.starts[0]["video_url"] == "https://example/a"
    assert controller.stops == 1
    assert controller.rois[0][0] == "video"
    assert controller.selections == [2, None]
    assert controller.lines == ["高清直播5"]
    with urllib.request.urlopen(api_request(running_server, "/api/snapshot/video.jpg")) as response:
        assert response.headers.get_content_type() == "image/jpeg"
        assert response.read() == b"jpeg-fixture"


@pytest.mark.parametrize(
    "value", [None, [], "", "https://cdn.example/live.m3u8", "高清\n直播5", "x" * 81]
)
def test_invalid_line_names_do_not_reach_controller(running_server, value):
    controller, _, _ = running_server
    request = api_request(
        running_server, "/api/line", data=json.dumps({"text": value}).encode(), method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400 and not controller.lines


def test_web_assets_are_served_locally(running_server):
    _, base, server = running_server
    with urllib.request.urlopen(base + "/") as response:
        html = response.read().decode()
        assert 'src="/static/vendor/hls.min.js"' in html
        assert 'src="https://' not in html
        assert server.access_token not in html
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "media-src 'self' blob:" in response.headers["Content-Security-Policy"]
    for asset, content_type in (
        ("app.js", "text/javascript"),
        ("style.css", "text/css"),
        ("vendor/hls.min.js", "text/javascript"),
    ):
        with urllib.request.urlopen(base + "/static/" + asset) as response:
            assert response.headers.get_content_type() == content_type
            assert len(response.read()) > 100
    request = urllib.request.Request(base + "/", method="HEAD")
    with urllib.request.urlopen(request) as response:
        assert response.read() == b""


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/status"),
        ("HEAD", "/api/status"),
        ("GET", "/api/snapshot/video.jpg"),
        ("GET", "/api/snapshot/bili.jpg"),
    ]
    + [
        ("POST", f"/api/{action}")
        for action in (
            "start",
            "stop",
            "offset",
            "audio",
            "roi",
            "line",
            "source",
            "remeasure",
            "resniff",
        )
    ],
)
def test_all_control_routes_require_the_current_token(running_server, method, path):
    controller, base, _ = running_server
    request = urllib.request.Request(base + path, method=method)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401
    assert error.value.headers["WWW-Authenticate"].startswith("Bearer ")
    assert "Access-Control-Allow-Origin" not in error.value.headers
    assert not any(
        (
            controller.starts,
            controller.stops,
            controller.deltas,
            controller.rois,
            controller.audio,
            controller.lines,
            controller.selections,
            controller.remeasure,
            controller.resniff,
        )
    )


def test_wrong_or_query_string_tokens_cannot_authorize(running_server):
    _, base, server = running_server
    requests = [
        api_request(running_server, "/api/status", headers={"Authorization": "Bearer wrong"}),
        urllib.request.Request(base + f"/api/status?token={server.access_token}"),
    ]
    for request in requests:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401


@pytest.mark.parametrize("origin", ["http://untrusted.example", "null", "http://127.0.0.1:1"])
def test_cross_origin_control_is_rejected_even_with_token(running_server, origin):
    controller, _, _ = running_server
    request = api_request(running_server, "/api/stop", data=b"{}", headers={"Origin": origin})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 403
    assert "Access-Control-Allow-Origin" not in error.value.headers
    assert controller.stops == 0


def test_same_origin_control_and_non_browser_clients_work(running_server):
    controller, base, server = running_server
    for headers in (
        {},
        {"Origin": base, "Sec-Fetch-Site": "same-origin"},
        {"Authorization": f"bearer {server.access_token}"},
    ):
        with urllib.request.urlopen(
            api_request(running_server, "/api/stop", data=b"{}", headers=headers)
        ) as response:
            assert response.status == 202
    assert controller.stops == 3


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_simple_post_content_types_cannot_mutate_state(running_server, content_type):
    controller, _, _ = running_server
    request = api_request(
        running_server, "/api/stop", data=b"{}", headers={"Content-Type": content_type}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 415
    assert controller.stops == 0


def test_preflight_is_allowed_only_for_media(running_server):
    _, base, _ = running_server
    headers = {"Origin": "http://untrusted.example", "Access-Control-Request-Method": "POST"}
    request = urllib.request.Request(base + "/api/start", method="OPTIONS", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 403
    assert "Access-Control-Allow-Origin" not in error.value.headers
    request = urllib.request.Request(base + "/live.m3u8", method="OPTIONS", headers=headers)
    with urllib.request.urlopen(request) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" not in response.headers["Access-Control-Allow-Methods"]


def test_restart_rotates_the_control_token(running_server):
    _, base, server = running_server
    previous = api_request(running_server, "/api/status")
    old_token = server.access_token
    server.stop()
    server.start()
    assert server.access_token != old_token
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(previous)
    assert error.value.code == 401
    with urllib.request.urlopen(api_request(running_server, "/api/status")) as response:
        assert response.status == 200


def test_static_assets_can_be_revalidated(running_server):
    _, base, _ = running_server
    with urllib.request.urlopen(base + "/static/app.js") as response:
        etag = response.headers["ETag"]
        assert response.headers["Cache-Control"] == "no-cache"
    request = urllib.request.Request(base + "/static/app.js", headers={"If-None-Match": etag})
    with pytest.raises(urllib.error.HTTPError) as response:
        urllib.request.urlopen(request)
    assert response.value.code == 304
    assert response.value.read() == b""


@pytest.mark.parametrize(
    "method,range_header,expected_size",
    [("HEAD", None, 0), ("GET", "bytes=3-18", 16), ("GET", None, FILE_CHUNK_SIZE * 3 + 7)],
)
def test_media_reads_are_bounded_and_head_reads_no_content(
    running_server, monkeypatch, method, range_header, expected_size
):
    _, base, server = running_server
    path = server.hls_dir / "seg_000001.ts"
    content = b"x" * (FILE_CHUNK_SIZE * 3 + 7)
    path.write_bytes(content)
    opened, sizes = Path.open, []

    class ObservedFile:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size=-1):
            assert 0 < size <= FILE_CHUNK_SIZE
            result = self.handle.read(size)
            sizes.append(len(result))
            return result

    def observe(candidate, *args, **kwargs):
        handle = opened(candidate, *args, **kwargs)
        return ObservedFile(handle) if candidate == path else handle

    monkeypatch.setattr(Path, "open", observe)
    request = urllib.request.Request(
        base + "/seg_000001.ts",
        method=method,
        headers={"Range": range_header} if range_header else {},
    )
    with urllib.request.urlopen(request) as response:
        assert response.read() == b"x" * expected_size
        assert int(response.headers["Content-Length"]) == (
            len(content) if method == "HEAD" else expected_size
        )
    assert sum(sizes) == expected_size


@pytest.mark.parametrize(
    "requested",
    ["bytes=-0", "bytes=4-2", "bytes=99-", "bytes=0-1,3-4", "bytes=-", "bytes=" + "1" * 5000 + "-"],
)
def test_invalid_media_ranges_return_416(running_server, requested):
    _, base, _ = running_server
    request = urllib.request.Request(base + "/seg_000001.ts", headers={"Range": requested})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 416
