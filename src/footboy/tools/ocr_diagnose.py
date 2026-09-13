"""Diagnose a local keyframe NPZ without opening a stream or changing saved settings."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np

from footboy.i18n import ArgumentParser, configure_cli_language, exception_message, localize, tr
from footboy.probe.ocr import (
    FLIPS,
    STYLES,
    OcrError,
    OcrProgress,
    ProbeConfig,
    make_backend,
    probe_clock,
)


def main(argv: list[str] | None = None) -> int:
    configure_cli_language(argv)
    parser = ArgumentParser(
        description=tr(
            "Diagnose a local keyframe NPZ without opening a stream or changing saved settings."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("frames", type=Path, help=tr("NPZ with pts and frames arrays"))
    parser.add_argument(
        "--output", type=Path, required=True, help=tr("New local diagnostic directory")
    )
    parser.add_argument("--start", type=int, default=0, help=tr("First frame index (default: 0)"))
    parser.add_argument(
        "--count", type=int, default=4, help=tr("Number of frames, at least 3 (default: 4)")
    )
    parser.add_argument(
        "--budget", type=float, default=15, help=tr("OCR time budget in seconds (default: 15)")
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "tesseract", "rapidocr"),
        default="auto",
        help=tr("OCR engine (default: auto)"),
    )
    parser.add_argument(
        "--tesseract-command", help=tr("Executable path; by default search PATH, then tools")
    )
    parser.add_argument(
        "--roi",
        nargs=4,
        type=float,
        metavar=("X", "Y", "W", "H"),
        help=tr("Clock region in normalized coordinates (0–1)"),
    )
    parser.add_argument(
        "--flip", choices=FLIPS, default="none", help=tr("Frame orientation (default: none)")
    )
    parser.add_argument(
        "--style",
        choices=STYLES,
        default="standard",
        help=tr("OCR preprocessing style (default: standard)"),
    )
    parser.add_argument(
        "--inverted", action="store_true", help=tr("Invert colors for clock recognition")
    )
    args = parser.parse_args(argv)
    if args.start < 0 or args.count < 3 or not math.isfinite(args.budget) or args.budget <= 0:
        parser.error(tr("start must be nonnegative, count >= 3, and budget positive and finite"))
    try:
        saved = (
            ProbeConfig(tuple(args.roi), args.flip, args.inverted, args.style) if args.roi else None
        )
        with np.load(args.frames, allow_pickle=False) as archive:
            pts, images = archive["pts"], archive["frames"]
        if (
            pts.ndim != 1
            or images.ndim not in (3, 4)
            or (images.ndim == 4 and images.shape[-1] != 3)
            or images.dtype != np.uint8
            or min(images.shape[1:3]) < 1
            or len(pts) != len(images)
            or args.start + args.count > len(pts)
        ):
            raise ValueError(
                tr("Expected matching PTS and uint8 grayscale/BGR frames in the chosen range")
            )
        frames = [
            (float(pts[index]), images[index])
            for index in range(args.start, args.start + args.count)
        ]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(exception_message(exc))

    # Diagnostic frames and OCR text stay local, including when output is inside
    # a checkout. Refuse to replace an earlier run's artifacts.
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(exception_message(exc))
    (args.output / ".gitignore").write_text("*\n", encoding="utf-8")
    last = OcrProgress("starting")
    with (args.output / "events.jsonl").open("w", encoding="utf-8") as stream:

        def record(event: OcrProgress) -> None:
            nonlocal last
            last = event
            row = event.public_dict()
            for kind, image in (("crop", event.crop), ("processed", event.image)):
                if image is not None and image.size:
                    filename = f"{event.attempts:04d}-frame-{event.frame_index}-{kind}.png"
                    if not cv2.imwrite(str(args.output / filename), image):
                        raise OSError(tr("Cannot write diagnostic {0}", filename))
                    row[kind] = filename
            stream.write(json.dumps(localize(row), ensure_ascii=False) + "\n")
            stream.flush()

        try:
            backend = make_backend(args.backend, tesseract_command=args.tesseract_command)
            result = probe_clock(
                frames, backend, saved=saved, budget=args.budget, on_progress=record
            )
            summary = {
                "ok": True,
                "config": result.config.to_dict(),
                "clocks": [sample.clock for sample in result.samples],
                "k": result.k,
                "residual": result.residual,
            }
        except OcrError as exc:
            summary = {"ok": False, "error": type(exc).__name__, "message": exception_message(exc)}
    summary.update(attempts=last.attempts, seconds=round(last.elapsed, 3))
    output = json.dumps(localize(summary), ensure_ascii=False, indent=2) + "\n"
    (args.output / "result.json").write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
