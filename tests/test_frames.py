from __future__ import annotations

import sys
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from footboy.probe.frames import FrameProbeError, keyframes
from footboy.sources.models import Source


def install_decoder(monkeypatch, timestamps):
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    stream = SimpleNamespace(
        time_base=Fraction(1, 1000), codec_context=SimpleNamespace(skip_frame=None)
    )
    frames = [
        SimpleNamespace(pts=pts, time_base=Fraction(1, 90000), to_ndarray=lambda **_: image)
        for pts in timestamps
    ]
    closed, opened = [], []
    container = SimpleNamespace(
        streams=SimpleNamespace(video=[stream]),
        decode=lambda _: iter(frames),
        close=lambda: closed.append(True),
    )

    def av_open(url, **kwargs):
        opened.append((url, kwargs))
        return container

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=av_open))
    return closed, opened, stream


def test_cached_keyframes_use_frame_time_base_and_never_zero_source_pts(monkeypatch):
    closed, opened, stream = install_decoder(monkeypatch, [9000000, 9450000, 9900000, 10350000])
    source = Source("https://cdn.example/live.flv", headers={"Cookie": "sid=x"})
    frames = list(keyframes(source, max_frames=4))
    assert [pts for pts, _ in frames] == [100, 105, 110, 115]
    assert stream.codec_context.skip_frame == "NONKEY"
    assert opened[0][1]["options"]["headers"] == "Cookie: sid=x\r\n"
    assert closed == [True]


def test_pts_reset_during_capture_is_rejected_and_container_closed(monkeypatch):
    closed, _, _ = install_decoder(monkeypatch, [9000000, 9450000, 0])
    with pytest.raises(FrameProbeError, match="PTS 回退"):
        list(keyframes(Source("https://cdn.example/live.flv"), max_frames=4))
    assert closed == [True]
