from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from footboy.serve.http import ControlServer


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

    def public_status(self):
        return {"state": "RUN", "offset_seconds": -1.5, "aligned": True}

    def request_offset_delta(self, delta_ms: int) -> None:
        self.deltas.append(delta_ms)

    def request_remeasure(self) -> None:
        self.remeasure += 1

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
        yield controller, f"http://127.0.0.1:{server.port}"
    finally:
        server.stop()


def test_status_hls_mime_and_cors(running_server) -> None:
    _, base = running_server
    with urllib.request.urlopen(base + "/api/status") as response:
        assert json.load(response)["state"] == "RUN"
        assert response.headers["Access-Control-Allow-Origin"] == "*"
    with urllib.request.urlopen(base + "/live.m3u8") as response:
        assert response.headers.get_content_type() == "application/vnd.apple.mpegurl"
        assert response.headers["Cache-Control"] == "no-store"
    head = urllib.request.Request(base + "/seg_000001.ts", method="HEAD")
    with urllib.request.urlopen(head) as response:
        assert response.headers.get_content_type() == "video/mp2t"
        assert response.headers["Content-Length"] == "7"


def test_control_actions_and_validation(running_server) -> None:
    controller, base = running_server
    request = urllib.request.Request(
        base + "/api/offset",
        data=b'{"delta_ms":-500}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        assert response.status == 202
    assert controller.deltas == [-500]

    bad = urllib.request.Request(
        base + "/api/offset",
        data=b'{"delta_ms":"oops"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(bad)
    assert error.value.code == 400


def test_path_traversal_is_not_served(running_server) -> None:
    _, base = running_server
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
    _, base = running_server
    request = urllib.request.Request(base + "/api/offset", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400
    assert json.load(error.value)["ok"] is False


def test_media_range_requests_and_invalid_range(running_server):
    _, base = running_server
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
    controller, base = running_server
    for route, value in (
        ("start", {"video_url": "https://example/a", "bili_url": "https://live.bilibili.com/0"}),
        ("stop", {}),
        ("roi", {"source": "video", "roi": [0, 0, 0.2, 0.2], "flip": "h", "inverted": False}),
        ("source", {"id": 2}),
        ("source", {"id": None}),
        ("line", {"text": "高清直播5"}),
    ):
        request = urllib.request.Request(
            base + "/api/" + route, data=json.dumps(value).encode(), method="POST"
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
    assert controller.starts[0]["video_url"] == "https://example/a"
    assert controller.stops == 1
    assert controller.rois[0][0] == "video"
    assert controller.selections == [2, None]
    assert controller.lines == ["高清直播5"]
    with urllib.request.urlopen(base + "/api/snapshot/video.jpg") as response:
        assert response.headers.get_content_type() == "image/jpeg"
        assert response.read() == b"jpeg-fixture"


@pytest.mark.parametrize(
    "value", [None, [], "", "https://cdn.example/live.m3u8", "高清\n直播5", "x" * 81]
)
def test_invalid_line_names_do_not_reach_controller(running_server, value):
    controller, base = running_server
    request = urllib.request.Request(
        base + "/api/line", data=json.dumps({"text": value}).encode(), method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400 and not controller.lines


def test_web_assets_are_served_locally(running_server):
    _, base = running_server
    with urllib.request.urlopen(base + "/") as response:
        html = response.read().decode()
        assert 'src="/static/vendor/hls.min.js"' in html
        assert 'src="https://' not in html
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
