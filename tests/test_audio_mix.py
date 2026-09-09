from __future__ import annotations

import pytest
from test_supervisor import finish, supervisor_at

from footboy.mux.ffmpeg import AudioMix, build_ffmpeg_command
from footboy.sources.models import Source


@pytest.mark.parametrize("value", [True, "0.5", -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_audio_volume_rejected(value):
    with pytest.raises(ValueError):
        AudioMix(commentary_volume=value)


def test_audio_channels_are_independent_and_do_not_invalidate_ocr(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("video.flv", has_audio=True)
    supervisor.bili = Source("bili.flv", has_audio=True)
    supervisor.request_audio({"original_enabled": True, "original_volume": 0.25})
    supervisor.request_audio({"commentary_volume": 0})
    assert supervisor.audio == AudioMix(True, 0.25, 0)
    assert supervisor._revision == 0
    finish(supervisor, -4719, mode="manual")
    assert supervisor.offset == -4719
    assert supervisor.audio == AudioMix(True, 0.25, 0)
    monkeypatch.setattr(supervisor.muxer, "restart", lambda *a: None)
    supervisor._apply_adjustment()
    assert supervisor.muxer.audio == supervisor.audio
    assert supervisor._adjust_deadline is None


def test_audio_change_during_restart_is_not_lost(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("video.flv", has_audio=True)
    supervisor.bili = Source("bili.flv", has_audio=True)
    supervisor.request_audio({"commentary_volume": 0.5})
    monkeypatch.setattr(
        supervisor.muxer, "restart", lambda *a: supervisor.request_audio({"original_volume": 0.3})
    )
    supervisor._apply_adjustment()
    assert supervisor._adjust_deadline is not None
    assert supervisor.audio == AudioMix(False, 0.3, 0.5)


def test_original_audio_requires_track_and_unknown_fields_are_rejected(tmp_path):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("video.flv", has_audio=False)
    with pytest.raises(ValueError):
        supervisor.request_audio({"original_enabled": True})
    with pytest.raises(ValueError):
        supervisor.request_audio({"volume": 0.5})
    supervisor.request_audio({"commentary_volume": 0.5})
    assert supervisor.audio.commentary_volume == 0.5


def test_mix_preserves_video_copy_and_offset(tmp_path):
    video = Source("video.flv", has_audio=True)
    bili = Source("bili.flv", audio_codec="aac")
    cmd = build_ffmpeg_command(
        video, bili, -4719, tmp_path, audio=AudioMix(True, 0.2, 0.8), audio_origin=1000
    )
    assert cmd[cmd.index("-itsoffset") + 1] == "-4719"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "normalize=0" in cmd[cmd.index("-filter_complex") + 1]


def test_manual_sync_refreshes_output_even_when_offset_is_unchanged(tmp_path):
    supervisor = supervisor_at(tmp_path, initial_offset=10)
    supervisor.applied_offset = 10
    finish(supervisor, 10, mode="manual")
    assert supervisor._adjust_deadline is not None
