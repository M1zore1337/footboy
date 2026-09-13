from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from footboy.i18n import tr
from footboy.sources.models import Source


class AudioCorrelationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PcmCapture:
    first_pts: float
    samples: np.ndarray
    sample_rate: int = 8000


@dataclass(frozen=True, slots=True)
class AudioOffsetResult:
    offset: float
    lag: float
    peak_ratio: float


def capture_pcm(source: Source, *, duration: float = 90.0, sample_rate: int = 8000) -> PcmCapture:
    if not 60 <= duration <= 120:
        raise ValueError(tr("Audio correlation capture must last between 60 and 120 seconds"))
    try:
        import av
    except ImportError as exc:
        raise AudioCorrelationError(tr("PyAV is not installed")) from exc
    try:
        container = av.open(source.url, options=source.pyav_options())
    except Exception as exc:
        raise AudioCorrelationError(
            tr("Cannot open the audio stream from {0}: {1}", source.domain, exc)
        ) from exc
    arrays: list[np.ndarray] = []
    first_pts: float | None = None
    collected = 0
    target = round(duration * sample_rate)
    try:
        if not container.streams.audio:
            raise AudioCorrelationError(tr("{0} has no audio", source.domain))
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            time_base = frame.time_base or stream.time_base
            if first_pts is None and frame.pts is not None and time_base is not None:
                first_pts = float(frame.pts * time_base)
            for converted in resampler.resample(frame):
                values = converted.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
                arrays.append(values)
                collected += len(values)
            if collected >= target:
                break
    except AudioCorrelationError:
        raise
    except Exception as exc:
        raise AudioCorrelationError(
            tr("Failed to capture PCM from {0}: {1}", source.domain, exc)
        ) from exc
    finally:
        container.close()
    if first_pts is None or collected < sample_rate * 10:
        raise AudioCorrelationError(tr("Too few audio samples for correlation"))
    return PcmCapture(first_pts, np.concatenate(arrays)[:target], sample_rate)


def correlate_offset(
    video: PcmCapture,
    bili: PcmCapture,
    *,
    max_lag: float = 100.0,
    min_peak_ratio: float = 1.25,
) -> AudioOffsetResult:
    if video.sample_rate != bili.sample_rate:
        raise ValueError(tr("Both PCM streams must use the same sample rate"))
    try:
        from scipy.signal import butter, sosfilt  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise AudioCorrelationError(tr("Audio correlation requires scipy")) from exc
    sample_rate = video.sample_rate
    sos = butter(6, [300, 3000], btype="bandpass", fs=sample_rate, output="sos")
    first = sosfilt(sos, video.samples)
    second = sosfilt(sos, bili.samples)
    lag_samples, ratio = _gcc_phat(first, second, round(max_lag * sample_rate))
    if not math.isfinite(ratio) or ratio < min_peak_ratio:
        raise AudioCorrelationError(
            tr("Ambiguous cross-correlation peak (peak/runner-up={0:.2f})", ratio)
        )
    lag = lag_samples / sample_rate
    return AudioOffsetResult(
        offset=(video.first_pts - bili.first_pts) + lag,
        lag=lag,
        peak_ratio=ratio,
    )


def _gcc_phat(first: np.ndarray, second: np.ndarray, max_shift: int) -> tuple[int, float]:
    size = 1 << (len(first) + len(second) - 1).bit_length()
    first_fft = np.fft.rfft(first, n=size)
    second_fft = np.fft.rfft(second, n=size)
    cross = first_fft * np.conj(second_fft)
    cross /= np.maximum(np.abs(cross), 1e-12)
    correlation = np.fft.irfft(cross, n=size)
    max_shift = min(max_shift, size // 2)
    window = np.concatenate((correlation[-max_shift:], correlation[: max_shift + 1]))
    magnitudes = np.abs(window)
    peak_index = int(np.argmax(magnitudes))
    lag = peak_index - max_shift
    peak = float(magnitudes[peak_index])
    exclusion = max(8, int(0.02 * max_shift))
    masked = magnitudes.copy()
    masked[max(0, peak_index - exclusion) : peak_index + exclusion + 1] = 0
    second_peak = float(np.max(masked)) if masked.size else 0.0
    ratio = peak / max(second_peak, 1e-12)
    return lag, ratio
