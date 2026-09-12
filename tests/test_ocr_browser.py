"""Browser feedback with controlled OCR states; no real stream is opened."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from footboy.probe.ocr import ProbeConfig
from footboy.serve.http import ControlServer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.environ.get("FOOTBOY_BROWSER_TESTS") != "1", reason="需要启用浏览器回归"),
]


class OcrController:
    def __init__(self):
        self.lock = threading.RLock()
        config = ProbeConfig((0.1, 0.1, 0.25, 0.12), "none", False).to_dict()
        self.status = {
            "state": "RUN",
            "active": True,
            "session_id": "ocr-feedback",
            "aligned": False,
            "offset_seconds": 0,
            "video": {"domain": "video.example"},
            "bili": {"domain": "bili.example"},
            "ffmpeg": {"running": False},
            "capabilities": {"roi": True},
            "measurement": {
                "running": True,
                "needs_roi": [],
                "sources": {
                    label: {"available": True, "version": 1, "config": config, "ocr": {}}
                    for label in ("video", "bili")
                },
            },
        }
        frame = np.full((360, 640, 3), (64, 160, 32), dtype=np.uint8)
        self.jpeg = cv2.imencode(".jpg", frame)[1].tobytes()
        self.png = cv2.imencode(".png", frame[36:79, 64:224])[1].tobytes()
        self.set_progress("video", "reading", clock=3714)
        self.set_progress("bili", "not_found", reason="未读到连续时钟")

    def public_status(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def preview(self, label):
        return self.jpeg

    def ocr_image(self, label, kind, version):
        with self.lock:
            reading = self.status["measurement"]["sources"][label]["ocr"].get("reading", {})
            return self.png if reading.get("version") == version else None

    def set_progress(self, label, state, *, clock=None, reason=None, samples=0):
        with self.lock:
            meta = self.status["measurement"]["sources"][label]
            previous = meta["ocr"].get("reading")
            reading = (
                {
                    "clock": clock,
                    "text": "00:00" if clock == 0 else "61:54",
                    "frame_index": 2,
                    "version": f"{label}-{clock}",
                    "seconds": 0.1,
                    "config": meta["config"],
                }
                if clock is not None
                else previous
            )
            meta["ocr"] = {"state": state, "samples": samples, "reading": reading, "reason": reason}

    def request_roi(self, label, value):
        with self.lock:
            meta = self.status["measurement"]["sources"][label]
            meta["config"] = ProbeConfig.from_dict(value).to_dict()
            meta["ocr"] = {"state": "queued"}
            self.status["measurement"]["running"] = True


@pytest.mark.parametrize("viewport", [(1440, 1080), (390, 844)], ids=["desktop", "mobile"])
def test_ocr_preview_distinguishes_a_single_reading_from_continuous_lock(tmp_path, viewport):
    from playwright.sync_api import expect, sync_playwright

    controller = OcrController()
    server = ControlServer(controller, tmp_path, host="127.0.0.1", port=0)
    server.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    errors.append(message.text)
                    if "Content Security Policy" in message.text
                    else None
                ),
            )
            page.goto(f"http://127.0.0.1:{server.port}/#token={server.access_token}")
            expect(page.locator("#roi-empty")).to_be_hidden()
            expect(page.locator("#roi-crop-preview")).to_be_visible()
            expect(page.locator("#ocr-reading-state")).to_contain_text("61:54")
            expect(page.locator("#ocr-validation-state")).to_contain_text("尚未锁定")
            expect(page.locator("#alignment")).to_have_text("未对齐")
            pixel = page.locator("#roi-crop").evaluate(
                "c => Array.from(c.getContext('2d').getImageData(0, 0, 1, 1).data)"
            )
            assert pixel[:3] == pytest.approx([32, 160, 64], abs=3)
            page.locator("#ocr-diagnostics > summary").click()
            expect(page.locator("#ocr-actual-crop")).to_be_visible()
            expect(page.locator("#ocr-processed")).to_be_visible()
            expect(page.locator("#ocr-details")).to_contain_text("第 3 帧")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            for state, text in (
                ("stopped", "停表"),
                ("timeout", "超时"),
                ("error", "失败"),
                ("not_found", "框选"),
            ):
                controller.set_progress("video", state, reason=text)
                expect(page.locator("#ocr-validation-state")).to_contain_text(text)
                expect(page.locator("#alignment")).to_have_text("未对齐")

            controller.set_progress("video", "reading", clock=0)
            expect(page.locator("#ocr-reading-state")).to_contain_text("00:00")
            controller.set_progress("video", "locked", samples=4)
            expect(page.locator("#ocr-validation-state")).to_contain_text("连续 4 帧走表通过")
            expect(page.locator("#alignment")).to_have_text("未对齐")
            page.locator("#tab-bili").click()
            expect(page.locator("#ocr-validation-state")).to_contain_text("框选")
            page.locator("#tab-video").click()
            expect(page.locator("#ocr-reading-state")).to_contain_text("00:00")

            screenshots = os.environ.get("FOOTBOY_SCREENSHOT_DIR")
            if screenshots:
                folder = Path(screenshots)
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(folder / f"ocr-feedback-{viewport[0]}.png"), full_page=True
                )
            page.locator(".fine-roi > summary").click()
            page.locator("#roi-x").fill("12")
            expect(page.locator("#ocr-validation-state")).to_have_text("当前选区尚未验证")
            expect(page.locator("#ocr-diagnostics")).to_be_hidden()
            # Keep a status response from before the save in flight. Releasing
            # it afterwards must not restore the old ROI or its locked reading.
            page.evaluate(
                """async stale => {
                  while (refreshBusy) await new Promise(resolve => setTimeout(resolve, 10));
                  clearTimeout(refreshTimer);
                  const originalFetch = timedFetch;
                  timedFetch = (path, ...args) => {
                    if (path !== '/api/status') return originalFetch(path, ...args);
                    timedFetch = originalFetch;
                    return new Promise(resolve => {
                      window.releaseStatus = () => resolve(new Response(JSON.stringify(stale), {
                        status: 200, headers: {'Content-Type': 'application/json'}
                      }));
                    });
                  };
                  window.staleRefresh = refresh();
                }""",
                controller.public_status(),
            )
            page.locator("#save-roi").click()
            expect(page.locator("#ocr-validation-state")).to_contain_text("等待重新采样")
            page.evaluate(
                "() => { releaseStatus(); return staleRefresh.then(() => clearTimeout(refreshTimer)); }"
            )
            expect(page.locator("#roi-x")).to_have_value("12.0")
            expect(page.locator("#ocr-validation-state")).to_contain_text("等待重新采样")
            expect(page.locator("#ocr-diagnostics")).to_be_hidden()
            assert not errors
            browser.close()
    finally:
        server.stop()
