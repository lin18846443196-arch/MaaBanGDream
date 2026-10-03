"""Frame provenance for asynchronous screencaps, without inventing device time."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from numbers import Real

import numpy as np


@dataclass(frozen=True)
class FrameSample:
    image: np.ndarray
    capture_id: int
    request_started_at: float
    completed_at: float
    consumed_at: float
    is_new: bool
    source: str = "maa-screencap"

    @property
    def valid_metadata(self) -> bool:
        times = (self.request_started_at, self.completed_at, self.consumed_at)
        return (isinstance(self.capture_id, int) and not isinstance(self.capture_id, bool)
                and self.capture_id >= 0 and isinstance(self.is_new, bool)
                and all(isinstance(t, Real) and not isinstance(t, bool)
                        and math.isfinite(t) for t in times)
                and 0 <= self.request_started_at <= self.completed_at <= self.consumed_at)

    @property
    def captured_at(self) -> float:
        """Acquisition midpoint estimate, not a game renderer timestamp."""
        if not self.valid_metadata:
            return float("nan")
        return (self.request_started_at + self.completed_at) / 2.0

    @property
    def uncertainty_ms(self) -> float:
        if not self.valid_metadata:
            return float("inf")
        return max(0.0, (self.completed_at - self.request_started_at) * 500.0)

    @property
    def age_ms(self) -> float:
        if not self.valid_metadata:
            return float("inf")
        return max(0.0, (self.consumed_at - self.captured_at) * 1000.0)

    @property
    def eligible_for_start(self) -> bool:
        return (self.valid_metadata and self.is_new
                and self.age_ms <= 80.0 and self.uncertainty_ms <= 40.0)

    def diagnostics(self) -> dict[str, object]:
        result = {
            "capture_id": self.capture_id,
            "is_new": self.is_new,
            "request_started_at": self.request_started_at,
            "completed_at": self.completed_at,
            "captured_at": self.captured_at,
            "consumed_at": self.consumed_at,
            "age_ms": self.age_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "eligible_for_start": self.eligible_for_start,
            "source": self.source,
            "valid_metadata": self.valid_metadata,
            "time_basis": "request-completion-midpoint-estimate",
        }
        # Keep failure diagnostics strict JSON even for malformed replay data.
        for key, value in result.items():
            if isinstance(value, Real) and not math.isfinite(value):
                result[key] = None
        return result


class FrameSampleStatistics:
    """Bounded, in-memory diagnostics. Reused pixels never count as new FPS."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("frame sample capacity must be positive")
        self.consumed_samples = 0
        self.new_samples = 0
        self.reused_samples = 0
        self.ineligible_new_samples = 0
        self.invalid_samples = 0
        self.max_age_ms = 0.0
        self.max_uncertainty_ms = 0.0
        self.max_new_frame_gap_ms = 0.0
        self._ages: deque[float] = deque(maxlen=capacity)
        self._uncertainties: deque[float] = deque(maxlen=capacity)
        self._last_new_at: float | None = None
        self._first_new_at: float | None = None
        self._seen_capture_id: int | None = None

    def observe(self, sample: FrameSample) -> None:
        self.consumed_samples += 1
        if not sample.valid_metadata:
            self.invalid_samples += 1
            return
        self.max_age_ms = max(self.max_age_ms, sample.age_ms)
        self.max_uncertainty_ms = max(self.max_uncertainty_ms, sample.uncertainty_ms)
        self._ages.append(sample.age_ms)
        if not sample.is_new or (self._seen_capture_id is not None
                                 and sample.capture_id <= self._seen_capture_id):
            self.reused_samples += 1
            return
        self._seen_capture_id = sample.capture_id
        self.new_samples += 1
        self.ineligible_new_samples += int(not sample.eligible_for_start)
        self._uncertainties.append(sample.uncertainty_ms)
        if self._first_new_at is None:
            self._first_new_at = sample.captured_at
        if self._last_new_at is not None:
            self.max_new_frame_gap_ms = max(
                self.max_new_frame_gap_ms,
                max(0.0, (sample.captured_at - self._last_new_at) * 1000.0),
            )
        self._last_new_at = sample.captured_at

    def report(self) -> dict[str, object]:
        elapsed = (
            self._last_new_at - self._first_new_at
            if self._last_new_at is not None and self._first_new_at is not None
            else 0.0
        )
        return {
            "consumed_samples": self.consumed_samples,
            "new_samples": self.new_samples,
            "reused_samples": self.reused_samples,
            "ineligible_new_samples": self.ineligible_new_samples,
            "invalid_samples": self.invalid_samples,
            "reused_ratio": (
                self.reused_samples / self.consumed_samples
                if self.consumed_samples else 0.0
            ),
            "new_frame_fps": (self.new_samples - 1) / elapsed if elapsed > 0 else 0.0,
            "max_age_ms": self.max_age_ms,
            "max_uncertainty_ms": self.max_uncertainty_ms,
            "max_new_frame_gap_ms": self.max_new_frame_gap_ms,
            "age_p95_ms": float(np.percentile(self._ages, 95)) if self._ages else 0.0,
            "uncertainty_p95_ms": (
                float(np.percentile(self._uncertainties, 95))
                if self._uncertainties else 0.0
            ),
            "retained_samples": len(self._ages),
            "percentile_scope": (
                "full_run" if self.consumed_samples == len(self._ages) else "recent_window"
            ),
            "time_basis": "request-completion-midpoint-estimate",
            "renderer_timestamp_known": False,
        }
