from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from footboy.tools.ocr_diagnose import main


def test_local_diagnostics_record_candidate_images_text_and_validation(tmp_path, monkeypatch):
    source = tmp_path / "frames.npz"
    frames = np.stack([np.full((12, 32, 3), i * 2, dtype=np.uint8) for i in range(4)])
    np.savez(source, pts=np.arange(4) * 2.0, frames=frames)
    backend = SimpleNamespace(read=lambda image: f"01:{int(image[0, 0, 0]):02}")
    monkeypatch.setattr("footboy.tools.ocr_diagnose.make_backend", lambda *a, **k: backend)
    monkeypatch.setattr("footboy.probe.ocr.preprocess", lambda image, *args: image)
    monkeypatch.setattr("footboy.probe.ocr._text_rois", lambda *a, **k: [(0, 0, 1, 1)])
    monkeypatch.setattr("footboy.probe.ocr._candidate_rois", lambda: [])
    monkeypatch.setattr("footboy.probe.ocr._has_edges", lambda _: True)
    output = tmp_path / "diagnostics"
    arguments = [str(source), "--output", str(output)]
    assert main(arguments) == 0
    result = json.loads((output / "result.json").read_text())
    assert result["clocks"] == [60, 62, 64, 66] and result["attempts"] == 4
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert events[-1]["state"] == "locked" and events[-1]["samples"] == 4
    assert any(event["state"] == "candidate" and event["priority"] == 0 for event in events)
    for event in events:
        if event["state"] == "reading":
            assert event["text"] == f"01:{event['frame_index'] * 2:02}"
            crop = cv2.imread(str(output / event["crop"]))
            assert np.array_equal(crop, frames[event["frame_index"]])
            assert (output / event["processed"]).exists()
    assert (output / ".gitignore").read_text() == "*\n"
    before = (output / "events.jsonl").read_bytes()
    with pytest.raises(SystemExit):
        main(arguments)
    assert (output / "events.jsonl").read_bytes() == before


def test_diagnostic_timeout_is_recorded_as_failure(tmp_path, monkeypatch):
    source = tmp_path / "frames.npz"
    np.savez(source, pts=np.arange(4), frames=np.zeros((4, 12, 32, 3), dtype=np.uint8))
    now = [0.0]
    monkeypatch.setattr("footboy.probe.ocr.time.monotonic", lambda: now[0])

    def read(image):
        now[0] += 1
        return "61:54"

    monkeypatch.setattr(
        "footboy.tools.ocr_diagnose.make_backend", lambda *a, **k: SimpleNamespace(read=read)
    )
    output = tmp_path / "diagnostics"
    assert (
        main([str(source), "--output", str(output), "--budget", "0.5", "--roi", "0", "0", "1", "1"])
        == 1
    )
    result = json.loads((output / "result.json").read_text())
    assert result["ok"] is False and result["error"] == "OcrTimeout"
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert events[-1]["state"] == "timeout"
