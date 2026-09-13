"""Language changes preserve authentication, source inputs and active ROI edits."""

from __future__ import annotations

import copy
import os
import re

import cv2
import numpy as np
import pytest

from footboy.i18n import tr
from footboy.serve.http import ControlServer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("FOOTBOY_BROWSER_TESTS") != "1", reason="Enable browser tests"
    ),
]


class LanguageController:
    def __init__(self):
        self.starts = []
        self.status = {
            "state": "IDLE",
            "active": False,
            "session_id": 0,
            "aligned": False,
            "offset_seconds": 0,
            "ffmpeg": {"running": False},
            "capabilities": {"session_controls": True, "roi": True, "switch_line": True},
            "message": tr("Enter the match page and Bilibili room to connect"),
        }
        frame = np.full((180, 320, 3), (64, 120, 32), dtype=np.uint8)
        self.jpeg = cv2.imencode(".jpg", frame)[1].tobytes()

    def public_status(self):
        return copy.deepcopy(self.status)

    def request_start(self, body):
        self.starts.append(body)
        self.status.update(
            state="RUN",
            active=True,
            session_id=1,
            message=tr("Manual offset updated; applying in 1.5 seconds"),
            video={"domain": "video.example.invalid", "line_text": body["video_line_text"]},
            bili={"domain": "live.bilibili.com"},
            sniffer={
                "running": True,
                "lines": ["高清直播⑤"],
                "candidates": [
                    {"id": 1, "active": True, "url": "https://example.invalid/live.m3u8"}
                ],
            },
            measurement={
                "running": False,
                "sources": {
                    label: {
                        "available": True,
                        "version": 1,
                        "config": {"roi": [0.1, 0.1, 0.5, 0.3], "flip": "none", "inverted": False},
                        "ocr": {
                            "state": "not_found",
                            "reason": tr(
                                "Could not confirm a running clock across three consecutive frames"
                            ),
                        },
                    }
                    for label in ("video", "bili")
                },
            },
        )

    def preview(self, label):
        return self.jpeg


@pytest.fixture
def console(tmp_path):
    controller = LanguageController()
    server = ControlServer(controller, tmp_path, host="127.0.0.1", port=0)
    server.start()
    try:
        yield controller, f"http://127.0.0.1:{server.port}/#token={server.access_token}"
    finally:
        server.stop()


@pytest.mark.parametrize("width", [1440, 390])
def test_language_switch_preserves_inputs_session_and_roi(console, width):
    from playwright.sync_api import expect, sync_playwright

    controller, url = console
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": 1000}, locale="en-US")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        expect(page.locator("#access-panel")).to_be_hidden()
        expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
        expect(page.locator("#phase-text")).to_have_text("等待连接")
        page.locator("#language").select_option("en")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page).to_have_title("Footboy · Live Sync Console")
        expect(page.locator("#status-message")).to_have_text(
            "Enter the match page and Bilibili room to connect"
        )
        expect(page.locator("#video-url")).to_have_attribute(
            "placeholder", "Paste the full match page or media URL"
        )
        expect(page.locator("#copy-link")).to_have_attribute("aria-label", "Copy playback URL")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#video-url").fill("https://video.example.invalid/match")
        page.locator("#bili-url").fill("https://live.bilibili.com/1")
        page.locator("#video-line-text").fill("高清直播⑤")
        page.locator("#language").select_option("zh-CN")
        expect(page.locator("#video-url")).to_have_value("https://video.example.invalid/match")
        expect(page.locator("#video-line-text")).to_have_value("高清直播⑤")
        page.locator("#language").select_option("en")
        page.locator("#start").click()
        expect(page.locator("#phase-text")).to_have_text("Live stream running")
        expect(page.locator("#status-message")).to_have_text(
            "Manual offset updated; applying in 1.5 seconds"
        )
        expect(page.locator("#roi-empty")).to_be_hidden()
        expect(page.locator(".candidate-label")).to_have_text("Stream 1 · Playing")
        expect(page.locator("#current-line")).to_contain_text("Current: 高清直播⑤")
        page.locator(".fine-roi summary").click()
        page.locator("#roi-x").fill("15")
        expect(page.locator("#ocr-reading-state")).to_have_text(
            "Region changed; save to read the clock"
        )
        page.locator("#language").select_option("zh-CN")
        expect(page.locator("#status-message")).to_have_text("手动偏移已更新，1.5 秒后应用")
        expect(page.locator("#roi-x")).to_have_value(re.compile(r"15(?:\.0)?"))
        expect(page.locator("#ocr-reading-state")).to_have_text("选区已修改，保存后试读")
        page.locator("#language").select_option("en")
        expect(page.locator("#ocr-reading-state")).to_have_text(
            "Region changed; save to read the clock"
        )
        expect(page.locator("#roi-x")).to_have_value(re.compile(r"15(?:\.0)?"))
        assert len(controller.starts) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        other = browser.new_page()
        other.goto(url)
        expect(other.locator("html")).to_have_attribute("lang", "zh-CN")
        expect(other.locator("#status-message")).to_have_text("手动偏移已更新，1.5 秒后应用")
        page.reload()
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page.locator("#access-panel")).to_be_hidden()
        expect(page.locator("#status-message")).to_have_text(
            "Manual offset updated; applying in 1.5 seconds"
        )
        assert errors == []
        browser.close()


@pytest.mark.parametrize("storage", ["invalid", "disabled"])
def test_language_and_authentication_with_unavailable_or_invalid_storage(console, storage):
    from playwright.sync_api import expect, sync_playwright

    _, url = console
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(
            "localStorage.setItem('footboy.language', 'unsupported')"
            if storage == "invalid"
            else "Storage.prototype.getItem = Storage.prototype.setItem = () => { throw new Error('disabled'); };"
        )
        page.goto(url.split("#")[0])
        expect(page.locator("#language")).to_have_value("zh-CN")
        page.locator("#language").select_option("en")
        page.locator("#access-token").fill("invalid-token")
        page.locator("#access-submit").click()
        expect(page.locator("#access-message")).to_contain_text(
            "The control token is invalid or expired"
        )
        page.locator("#language").select_option("zh-CN")
        expect(page.locator("#access-message")).to_contain_text("控制密钥无效或已过期")
        page.goto(url)
        expect(page.locator("#access-panel")).to_be_hidden()
        page.locator("#language").select_option("en")
        expect(page.locator("#phase-text")).to_have_text("Ready to connect")
        browser.close()
