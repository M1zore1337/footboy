from __future__ import annotations

import ast
import copy
import json
import os
import string
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from footboy import i18n
from footboy.cli import main
from footboy.diagnostics import redact_diagnostic
from footboy.i18n import exception_message, localize, tr


@pytest.fixture(autouse=True)
def restore_language():
    previous = i18n.get_language()
    yield
    i18n.set_language(previous)


@pytest.mark.parametrize(
    ("module", "environment", "arguments", "expected"),
    [
        ("footboy", None, [], "启动 Footboy"),
        ("footboy", None, ["--lang", "en"], "Start the Footboy web console"),
        ("footboy", "en_US", [], "Start the Footboy web console"),
        ("footboy", "en", ["--lang", "zh-CN"], "启动 Footboy"),
        ("footboy", "unsupported", [], "启动 Footboy"),
        ("footboy.tools.p0", None, ["--lang=en"], "P0: validate two live inputs"),
        ("footboy.tools.ocr_diagnose", None, ["--lang=en"], "Diagnose a local keyframe NPZ"),
    ],
)
def test_cli_help_language_precedence(module, environment, arguments, expected):
    env = dict(os.environ)
    env.pop("FOOTBOY_LANG", None)
    if environment is not None:
        env["FOOTBOY_LANG"] = environment
    result = subprocess.run(
        [sys.executable, "-m", module, "--help", *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout and "--lang" in result.stdout
    if "启动" not in expected:
        assert not any("\u4e00" <= char <= "\u9fff" for char in result.stdout)


def test_cli_english_errors_include_translated_nested_causes(monkeypatch, capsys):
    def missing(*args, **kwargs):
        raise RuntimeError(tr("Cannot run {0}: {1}", "ffmpeg", tr("Unknown error")))

    monkeypatch.setattr("footboy.cli._check_binary", missing)
    assert main(["--lang", "en", "--check"]) == 2
    assert capsys.readouterr().err == "Environment check failed: Cannot run ffmpeg: Unknown error\n"
    with pytest.raises(SystemExit) as error:
        main(["--lang", "en", "--video-page", "https://example.invalid/match"])
    assert error.value.code == 2
    assert "Provide both --video-page and --bili-room, or omit both" in capsys.readouterr().err


def test_worker_messages_can_be_rendered_concurrently_without_changing_user_data():
    i18n.set_language("zh-CN")
    error = ValueError(tr("The match stream has no audio track"))
    message = tr("Session failed: {0}", exception_message(error))
    value = copy.deepcopy({"message": message, "line_text": "高清直播⑤", "ocr_text": "比赛 00:00"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        english, chinese = list(pool.map(lambda lang: localize(value, lang), ("en", "zh-CN")))
    assert english["message"] == "Session failed: The match stream has no audio track"
    assert chinese["message"] == "任务失败: 当前原直播不含音轨"
    assert english["line_text"] == chinese["line_text"] == "高清直播⑤"
    assert english["ocr_text"] == chinese["ocr_text"] == "比赛 00:00"
    assert i18n.get_language() == "zh-CN"
    assert str(message) == chinese["message"]


def test_combined_diagnostics_are_localized_and_redacted_in_both_languages():
    message = tr("No OCR backend is available; ") + tr("; ").join(
        [
            tr("Request failed: {0}", "https://u:secret@example.invalid/a?token=private"),
            tr("Unknown error"),
        ]
    )
    sanitized = redact_diagnostic(message)
    for language in i18n.LANGUAGES:
        text = localize(sanitized, language)
        assert "private" not in text and "secret" not in text and "<redacted>" in text
    assert "No OCR backend" in localize(sanitized, "en")
    assert "没有可用 OCR 后端" in localize(sanitized, "zh-CN")
    assert localize(tr("Uncatalogued fallback"), "en") == "Uncatalogued fallback"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "zh-CN"),
        ("fr, en-US;q=0.8, zh-CN;q=0.5", "en"),
        ("en;q=0.1, zh;q=0.8", "zh-CN"),
        ("en;q=0, zh-CN;q=1", "zh-CN"),
        ("en;q=nan, zh;q=0.5", "zh-CN"),
        ("en;q=invalid", "zh-CN"),
        ("unsupported", "zh-CN"),
    ],
)
def test_http_language_negotiation(header, expected):
    assert i18n.request_language(header) == expected


def test_python_catalog_preserves_format_fields_and_covers_application_messages():
    root = Path(i18n.__file__).parent
    catalog = json.loads((root / "locales/zh-CN.json").read_text("utf-8"))
    formatter = string.Formatter()

    def fields(text):
        return sorted(
            (name, spec, conv) for _, name, spec, conv in formatter.parse(text) if name is not None
        )

    for english, chinese in catalog.items():
        assert fields(english) == fields(chinese), english
        assert english.count("%s") == chinese.count("%s"), english
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
            ):
                assert isinstance(node.args[0], ast.Constant), path
                assert node.args[0].value in catalog, (path, node.args[0].value)
