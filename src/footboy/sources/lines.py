from __future__ import annotations

import re
import unicodedata
from typing import Any

LINE_SELECTOR = "a,button,[role=button],[onclick],[data-play]"
LINE_LABEL = re.compile(
    r"^(?:(?:中文|国语|英语|粤语|高清|超清|标清|蓝光|原画|流畅|主播|主队|客队|"
    r"备用|默认|解说|直播|线路|信号|频道|HD|SD|4K|8K)|[\d一二三四五六七八九十A-Za-z()（）_\- ])+$",
    re.IGNORECASE,
)


def normalize_line_text(text: str) -> str:
    """Treat circled/full-width digits and spacing as the same line name."""
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def validate_line_text(value: Any, *, optional: bool = False) -> str | None:
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError("线路名称必须是 1–80 个字符的文本")
    if any(ord(char) < 32 for char in value) or re.search(r"://|www\.", value, re.I):
        raise ValueError("请填写线路名称，不要填写地址或控制字符")
    return value.strip()


def is_line_label(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return bool(
        1 <= len(normalized) <= 40
        and LINE_LABEL.fullmatch(normalized)
        and re.search(
            r"高清|超清|标清|蓝光|原画|流畅|解说|直播|线路|信号|频道|备用|\b(?:HD|SD|4K|8K)\b",
            normalized,
            re.I,
        )
    )
