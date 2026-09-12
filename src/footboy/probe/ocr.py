from __future__ import annotations

import math
import re
import statistics
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol

import cv2
import numpy as np

from footboy.environment import resolve_binary

CLOCK_RE = re.compile(r"(?<![\d:：.])(\d{1,3})\s*[:：.]\s*(\d{2})(?![\d:：.])")
FLIPS = ("none", "h", "v", "hv")
STYLES = ("standard", "condensed")


class OcrError(RuntimeError):
    pass


class OcrTimeout(OcrError):
    pass


class OcrCancelled(OcrError):
    pass


class StoppedClock(OcrError):
    pass


class ManualSelectionRequired(OcrError):
    pass


class OcrBackend(Protocol):
    def read(self, image: np.ndarray) -> str: ...


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    roi: tuple[float, float, float, float]
    flip: str
    inverted: bool
    style: str = "standard"

    def __post_init__(self) -> None:
        if len(self.roi) != 4 or not all(math.isfinite(v) for v in self.roi):
            raise ValueError("ROI 必须包含四个有限数值")
        x, y, width, height = self.roi
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 1.000001
            or y + height > 1.000001
        ):
            raise ValueError("ROI 必须位于画面内，坐标范围为 0..1")
        if self.flip not in FLIPS or not isinstance(self.inverted, bool):
            raise ValueError("翻转或极性无效")
        if self.style not in STYLES:
            raise ValueError("OCR 预处理样式无效")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProbeConfig:
        roi = tuple(float(item) for item in value["roi"])
        if len(roi) != 4 or value.get("flip") not in FLIPS:
            raise ValueError("invalid probe config")
        return cls(
            roi=roi,  # type: ignore[arg-type]
            flip=str(value["flip"]),
            inverted=value["inverted"],
            style=value.get("style", "standard"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClockSample:
    pts: float
    clock: int


@dataclass(frozen=True, slots=True)
class ClockProbeResult:
    k: float
    samples: tuple[ClockSample, ...]
    residual: float
    config: ProbeConfig
    stopped: bool = False

    @property
    def representative_clock(self) -> float:
        return statistics.median(sample.clock for sample in self.samples)


class TesseractBackend:
    def __init__(self, command: str | None = None) -> None:
        try:
            import pytesseract  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise OcrError("未安装 pytesseract") from exc
        pytesseract.pytesseract.tesseract_cmd = resolve_binary(command or "tesseract")
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:
            raise OcrError("未找到 Tesseract 程序；请安装或指定 --tesseract-command") from exc
        self._module = pytesseract

    def read(self, image: np.ndarray, *, raw_line: bool = False) -> str:
        try:
            return str(
                self._module.image_to_string(
                    image,
                    config=f"--psm {13 if raw_line else 7} -c tessedit_char_whitelist=0123456789:",
                    timeout=2,
                )
            )
        except RuntimeError as exc:
            raise OcrError(f"Tesseract 识别超时或失败: {exc}") from exc


class RapidOcrBackend:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise OcrError("未安装 rapidocr-onnxruntime") from exc
        self._engine = RapidOCR()

    def read(self, image: np.ndarray) -> str:
        result, _ = self._engine(image)
        if not result:
            return ""
        return " ".join(str(row[1]) for row in result if len(row) > 1)


def make_backend(name: str = "auto", *, tesseract_command: str | None = None) -> OcrBackend:
    errors = []
    if name in {"auto", "rapidocr"}:
        try:
            return RapidOcrBackend()
        except Exception as exc:
            errors.append(str(exc))
            if name == "rapidocr":
                raise
    if name in {"auto", "tesseract"}:
        try:
            return TesseractBackend(tesseract_command)
        except OcrError as exc:
            errors.append(str(exc))
    raise OcrError("没有可用 OCR 后端；" + "；".join(errors))


def parse_clock(text: str) -> int | None:
    for match in CLOCK_RE.finditer(text):
        minutes, seconds = (int(value) for value in match.groups())
        if 0 <= minutes <= 130 and 0 <= seconds < 60:
            return minutes * 60 + seconds
    return None


def flip_frame(frame: np.ndarray, mode: str) -> np.ndarray:
    codes = {"none": None, "h": 1, "v": 0, "hv": -1}
    if mode not in codes:
        raise ValueError(f"unknown flip mode: {mode}")
    code = codes[mode]
    return frame if code is None else cv2.flip(frame, code)


def preprocess(crop: np.ndarray, inverted: bool, style: str = "standard") -> np.ndarray:
    condensed = style == "condensed"
    scaled = cv2.resize(crop, None, fx=8 if condensed else 4, fy=4, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY) if scaled.ndim == 3 else scaled
    mode = cv2.THRESH_BINARY_INV if inverted else cv2.THRESH_BINARY
    if condensed:
        # Small, bright broadcast digits need separation from antialiasing and
        # background graphics; Otsu can merge their narrow strokes and colon.
        _, binary = cv2.threshold(gray, 175, 255, mode)
        binary = cv2.copyMakeBorder(
            binary, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255 if inverted else 0
        )
    else:
        _, binary = cv2.threshold(gray, 0, 255, mode | cv2.THRESH_OTSU)
    return binary


def read_with_config(frame: np.ndarray, config: ProbeConfig, backend: OcrBackend) -> int | None:
    transformed = flip_frame(frame, config.flip)
    crop = _crop_normalized(transformed, config.roi)
    if crop.size == 0:
        return None
    image = preprocess(crop, config.inverted, config.style)
    if config.style == "condensed" and isinstance(backend, TesseractBackend):
        return parse_clock(backend.read(image, raw_line=True))
    return parse_clock(backend.read(image))


def probe_clock(
    frames: list[tuple[float, np.ndarray]],
    backend: OcrBackend,
    *,
    saved: ProbeConfig | None = None,
    allow_manual: bool = False,
    budget: float = 15.0,
    stop_event: threading.Event | None = None,
) -> ClockProbeResult:
    if len(frames) < 3:
        raise OcrError("至少需要 3 个关键帧才能锁定比赛时钟")
    deadline = time.monotonic() + budget

    def check_budget() -> None:
        if stop_event is not None and stop_event.is_set():
            raise OcrCancelled("测量已取消")
        if time.monotonic() >= deadline:
            raise OcrTimeout("自动识别超时，请重试或缩小识别区域")

    if saved:
        # Retry the selected region before considering any other location.
        # Backend failures, cancellation and timeouts are not evidence of a bad ROI.
        for style in (saved.style, *(item for item in STYLES if item != saved.style)):
            config = replace(saved, style=style)
            samples = _read_series(frames, config, backend, check_budget=check_budget)
            result = _validate_series(samples, config)
            if result:
                return result

    stopped = 0
    first_readings: dict[ProbeConfig, int] = {}
    for config in _discover_candidates(
        frames[0][1], backend, check_budget=check_budget, first_readings=first_readings
    ):
        samples = _read_series(
            frames,
            config,
            backend,
            check_budget=check_budget,
            first_clock=first_readings.get(config),
        )
        try:
            result = _validate_series(samples, config)
        except StoppedClock:
            stopped += 1
            continue
        if result:
            return result
    if stopped:
        raise StoppedClock("候选比赛时钟连续 3 帧不动，本轮视为停表")
    if not allow_manual:
        raise ManualSelectionRequired("自动 OCR 未锁定，请在网页框选比赛时钟")
    manual = _manual_config(frames[0][1], backend)
    result = _validate_series(_read_series(frames, manual, backend), manual)
    if not result:
        raise OcrError("手动框选区域仍无法连续识别比赛时钟")
    return result


def _discover_candidates(
    frame: np.ndarray,
    backend: OcrBackend,
    *,
    check_budget: Any = lambda: None,
    first_readings: dict[ProbeConfig, int] | None = None,
) -> Iterator[ProbeConfig]:
    candidates = []
    variants = (("standard", False), ("condensed", False), ("standard", True), ("condensed", True))
    for flip_index, flip in enumerate(FLIPS):
        check_budget()
        transformed = flip_frame(frame, flip)
        localized = [
            roi for roi in _text_rois(transformed) if _has_edges(_crop_normalized(transformed, roi))
        ]
        for rank, roi in enumerate(localized):
            for variant, (style, inverted) in enumerate(variants):
                priority = rank + 4 * variant + 4 * flip_index
                config = ProbeConfig(roi, flip, inverted, style)
                candidates.append((priority, variant, flip_index, len(candidates), config))
        for rank, roi in enumerate(_candidate_rois()):
            if roi not in localized and _has_edges(_crop_normalized(transformed, roi)):
                for variant, inverted in enumerate((False, True)):
                    priority = 24 + rank + 4 * variant + 4 * flip_index
                    config = ProbeConfig(roi, flip, inverted)
                    candidates.append((priority, variant, flip_index, len(candidates), config))

    # Interleave increasingly expensive alternatives with lower ranked regions.
    # A candidate just outside the leading group must not wait for every flip
    # and preprocessing combination of all the preceding background patches.
    for _, _, _, _, config in sorted(candidates):
        check_budget()
        clock = read_with_config(frame, config, backend)
        if clock is not None:
            if first_readings is not None:
                first_readings[config] = clock
            yield config


def _text_rois(frame: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Locate short text lines, keeping scoreboard neighbours outside the crop."""
    height, width = frame.shape[:2]
    if width > 1280:
        frame = cv2.resize(frame, (1280, round(height * 1280 / width)))
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    # Broadcast clocks can leave a wider gap around the colon. Keep the narrow
    # grouping for tiny clocks, and also join the complete spaced mm:ss line.
    kernels = [
        np.ones((1, max(2, round(width * gap))), dtype=np.uint8) for gap in (0.004, 0.006, 0.024)
    ]
    boxes = set()
    for threshold, mode in ((175, cv2.THRESH_BINARY), (80, cv2.THRESH_BINARY_INV)):
        _, mask = cv2.threshold(gray, threshold, 255, mode)
        mask[round(height * 0.25) : round(height * 0.38), :] = 0
        mask[round(height * 0.62) : round(height * 0.75), :] = 0
        mask[round(height * 0.38) : round(height * 0.62), : round(width * 0.24)] = 0
        mask[round(height * 0.38) : round(height * 0.62), round(width * 0.76) :] = 0
        for kernel in kernels:
            joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            # Dark digits inside a bright scoreboard are nested contours.
            retrieval = cv2.RETR_LIST if kernel.shape[1] > width * 0.01 else cv2.RETR_EXTERNAL
            contours, _ = cv2.findContours(joined, retrieval, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if not max(6, height * 0.008) <= h <= height * 0.08 or not 1.3 <= w / h <= 8:
                    continue
                pad_x, pad_y = max(2, math.ceil(h * 0.3)), max(2, math.ceil(h * 0.12))
                left, top = max(0, x - pad_x), max(0, y - pad_y)
                right, bottom = min(width, x + w + pad_x), min(height, y + h + pad_y)
                boxes.add((left, top, right - left, bottom - top))
    ranked = sorted(
        boxes,
        key=lambda box: (
            -_text_score(gray[box[1] : box[1] + box[3], box[0] : box[0] + box[2]]),
            box[1],
            box[0],
            box[2],
            box[3],
        ),
    )
    selected: list[tuple[int, int, int, int]] = []
    for box in ranked:
        if not any(_same_text_box(box, previous) for previous in selected):
            selected.append(box)
            if len(selected) == 64:
                break
    return [(x / width, y / height, w / width, h / height) for x, y, w, h in selected]


def _text_score(gray: np.ndarray) -> float:
    """Prefer aligned glyphs on a clean panel over textured broadcast backgrounds."""
    height = gray.shape[0]
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    border = np.concatenate((binary[0], binary[-1], binary[:, 0], binary[:, -1]))
    white_border = float(np.mean(border)) / 255
    foreground = cv2.bitwise_not(binary) if white_border > 0.5 else binary
    _, _, components, _ = cv2.connectedComponentsWithStats(foreground)
    glyphs = [
        (y, h)
        for _, y, w, h, area in components[1:]
        if h >= height * 0.45 and w <= height * 1.2 and area >= 3
    ]
    # These are ranking hints, not filters: narrow fonts can have joined digits,
    # faint colons, or three-digit minutes and must still reach the OCR backend.
    count_score = max(0, 1 - abs(len(glyphs) - 4) / 4)
    alignment = (
        max(0, 1 - float(np.std([y + h for y, h in glyphs])) / max(1, height * 0.25))
        if glyphs
        else 0
    )
    clean_border = abs(white_border - 0.5) * 2
    density = cv2.countNonZero(foreground) / foreground.size
    density_score = max(0, 1 - abs(density - 0.25) * 2)
    return 3 * count_score + 2 * alignment + 2 * clean_border + density_score


def _same_text_box(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    x, y, width, height = first
    other_x, other_y, other_width, other_height = second
    if abs(width - other_width) > max(2, min(width, other_width) * 0.1):
        return False
    if abs(height - other_height) > max(2, min(height, other_height) * 0.1):
        return False
    overlap = max(0, min(x + width, other_x + other_width) - max(x, other_x)) * max(
        0, min(y + height, other_y + other_height) - max(y, other_y)
    )
    return overlap / (width * height + other_width * other_height - overlap) >= 0.85


def _candidate_rois() -> list[tuple[float, float, float, float]]:
    result = []
    for width, height in ((0.36, 0.16), (0.52, 0.24)):
        anchors = (
            (0.0, 0.0),
            (1.0 - width, 0.0),
            ((1.0 - width) / 2, 0.0),
            (0.0, 1.0 - height),
            (1.0 - width, 1.0 - height),
            ((1.0 - width) / 2, 1.0 - height),
            ((1.0 - width) / 2, (1.0 - height) / 2),
        )
        result.extend((x, y, width, height) for x, y in anchors)
    return result


def _has_edges(crop: np.ndarray) -> bool:
    if crop.size == 0:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    edges = cv2.Canny(gray, 80, 160)
    return float(np.count_nonzero(edges)) / edges.size >= 0.008


def _crop_normalized(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    height, width = frame.shape[:2]
    x, y, w, h = roi
    left = max(0, min(width, round(x * width)))
    top = max(0, min(height, round(y * height)))
    right = max(left, min(width, round((x + w) * width)))
    bottom = max(top, min(height, round((y + h) * height)))
    return frame[top:bottom, left:right]


def _read_series(
    frames: list[tuple[float, np.ndarray]],
    config: ProbeConfig,
    backend: OcrBackend,
    *,
    check_budget: Any = lambda: None,
    first_clock: int | None = None,
) -> list[ClockSample | None]:
    result = []
    for index, (pts, frame) in enumerate(frames):
        check_budget()
        clock = (
            first_clock
            if index == 0 and first_clock is not None
            else read_with_config(frame, config, backend)
        )
        result.append(ClockSample(pts=pts, clock=clock) if clock is not None else None)
    return result


def _validate_series(
    samples: list[ClockSample | None], config: ProbeConfig
) -> ClockProbeResult | None:
    if len(samples) < 3:
        return None
    static_run = 0
    valid_run: list[ClockSample] = []
    best_run: list[ClockSample] = []
    previous: ClockSample | None = None
    for current in samples:
        if current is None or not math.isfinite(current.pts):
            if len(valid_run) > len(best_run):
                best_run = valid_run
            valid_run = []
            previous = None
            static_run = 0
            continue
        if previous is None:
            valid_run = [current]
            previous = current
            static_run = 1
            continue
        if current.clock == previous.clock and current.pts > previous.pts:
            static_run += 1
            if static_run >= 3:
                raise StoppedClock("比赛时钟连续 3 帧不动，本轮视为停表")
        else:
            static_run = 1
        delta_error = abs((current.clock - previous.clock) - (current.pts - previous.pts))
        if current.pts > previous.pts and delta_error <= 1.0:
            valid_run.append(current)
        else:
            if len(valid_run) > len(best_run):
                best_run = valid_run
            valid_run = [current]
        previous = current
    if len(valid_run) > len(best_run):
        best_run = valid_run
    if len(best_run) < 3:
        return None
    offsets = [sample.clock - sample.pts for sample in best_run]
    k = statistics.median(offsets)
    residual = max(abs(value - k) for value in offsets)
    if residual > 1.0:
        return None
    return ClockProbeResult(
        k=k,
        samples=tuple(best_run),
        residual=residual,
        config=config,
    )


def _manual_config(frame: np.ndarray, backend: OcrBackend) -> ProbeConfig:
    tiles = []
    labels = ("1 none", "2 h", "3 v", "4 hv")
    target_width = 640
    for mode, label in zip(FLIPS, labels, strict=True):
        image = flip_frame(frame, mode).copy()
        scale = target_width / image.shape[1]
        image = cv2.resize(image, (target_width, round(image.shape[0] * scale)))
        cv2.putText(image, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        tiles.append(image)
    top = np.hstack((tiles[0], tiles[1]))
    bottom = np.hstack((tiles[2], tiles[3]))
    montage = np.vstack((top, bottom))
    cv2.imshow("Footboy flips (choose 1-4 in terminal)", montage)
    cv2.waitKey(1)
    try:
        choice = input("OCR 自动锁定失败，请按画面选择翻转方向 1-4: ").strip()
        index = int(choice) - 1
        if index not in range(4):
            raise ValueError
    except ValueError as exc:
        cv2.destroyAllWindows()
        raise OcrError("翻转方向选择无效") from exc
    cv2.destroyAllWindows()
    mode = FLIPS[index]
    transformed = flip_frame(frame, mode)
    x, y, width, height = cv2.selectROI(
        "Footboy: drag around the match clock, then press Enter", transformed, False, False
    )
    cv2.destroyAllWindows()
    if width <= 0 or height <= 0:
        raise OcrError("没有选择 OCR 区域")
    frame_height, frame_width = transformed.shape[:2]
    roi = (x / frame_width, y / frame_height, width / frame_width, height / frame_height)
    for style in STYLES:
        for inverted in (False, True):
            config = ProbeConfig(roi=roi, flip=mode, inverted=inverted, style=style)
            if read_with_config(frame, config, backend) is not None:
                return config
    raise OcrError("框选区域未识别到 mm:ss")
