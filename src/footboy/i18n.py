"""Chinese by default, with explicit English selection for CLI and HTTP clients.

Messages keep both renderings until they reach an output boundary. This lets
two browsers use different languages while sharing the same session workers.
Only application messages are translated; URLs, stream labels and OCR text are
left as supplied by their sources.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES = ("zh-CN", "en")
DEFAULT_LANGUAGE = "zh-CN"
_language = DEFAULT_LANGUAGE


def normalize_language(value: str | None, default: str = DEFAULT_LANGUAGE) -> str:
    value = (value or "").strip().lower().replace("_", "-")
    if value == "en" or value.startswith("en-"):
        return "en"
    if value == "zh" or value.startswith("zh-"):
        return "zh-CN"
    return default


def request_language(header: str | None) -> str:
    """Choose a supported Accept-Language entry by quality, without global state."""
    preferences = []
    for entry in (header or "").split(","):
        tag, *parameters = entry.strip().split(";")
        quality = 1.0
        try:
            for parameter in parameters:
                if parameter.strip().startswith("q="):
                    quality = float(parameter.strip()[2:])
        except ValueError:
            continue
        language = normalize_language(tag, default="")
        if language and 0 < quality <= 1:
            preferences.append((quality, language))
    return max(preferences, key=lambda item: item[0])[1] if preferences else DEFAULT_LANGUAGE


def get_language() -> str:
    return _language


def set_language(language: str) -> None:
    global _language
    _language = normalize_language(language)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, str]:
    return json.loads((Path(__file__).with_name("locales") / "zh-CN.json").read_text("utf-8"))


class Message(str):
    """A string compatible with existing controllers, retaining both languages."""

    def __new__(cls, english: str, chinese: str) -> Message:
        value = super().__new__(cls, english if get_language() == "en" else chinese)
        value.english = english
        value.chinese = chinese
        return value

    def render(self, language: str) -> str:
        return self.english if language == "en" else self.chinese

    def __str__(self) -> str:
        return self.render(get_language())

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __getnewargs__(self) -> tuple[str, str]:
        return self.english, self.chinese

    def __add__(self, other: str) -> Message:
        return Message(
            self.english + localize(other, "en"), self.chinese + localize(other, "zh-CN")
        )

    def join(self, values: Iterable[str]) -> Message:
        values = list(values)
        return Message(
            self.english.join(localize(value, "en") for value in values),
            self.chinese.join(localize(value, "zh-CN") for value in values),
        )


def exception_message(error: BaseException) -> str:
    if len(error.args) == 1 and isinstance(error.args[0], Message):
        return error.args[0]
    return str(error)


def localize(value: Any, language: str | None = None) -> Any:
    """Render marked messages in API/diagnostic values, leaving user data alone."""
    language = language or get_language()
    if isinstance(value, Message):
        return value.render(language)
    if isinstance(value, BaseException):
        return localize(exception_message(value), language)
    if isinstance(value, dict):
        return {key: localize(item, language) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [localize(item, language) for item in value]
    return value


def tr(english: str, *args: object) -> Message:
    chinese = _catalog().get(english, english)
    if args:
        english = english.format(*(localize(arg, "en") for arg in args))
        chinese = chinese.format(*(localize(arg, "zh-CN") for arg in args))
    return Message(english, chinese)


def configure_cli_language(argv: list[str] | None) -> None:
    """Read the language before argparse renders help or validates other options."""
    selector = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selector.add_argument("--lang", choices=LANGUAGES)
    args, _ = selector.parse_known_args(argv)
    set_language(args.lang or normalize_language(os.environ.get("FOOTBOY_LANG")))


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "description" in kwargs:
            kwargs["description"] = localize(kwargs["description"])
        super().__init__(*args, **kwargs)
        self.add_argument(
            "--lang",
            choices=LANGUAGES,
            default=get_language(),
            help=tr("Output language (default: Chinese; override with FOOTBOY_LANG)"),
        )

    def add_argument(self, *args: Any, **kwargs: Any) -> Any:
        if "help" in kwargs:
            kwargs["help"] = localize(kwargs["help"])
        return super().add_argument(*args, **kwargs)

    def error(self, message: str) -> Any:
        return super().error(localize(message))
