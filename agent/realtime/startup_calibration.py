"""Explicit first-note timing equations and externally verified calibration.

Capture time means a host request/completion midpoint estimate. Neither a
device execution receipt nor the encoded FPS of a recording supplies the
independent game judgement truth required to calibrate a visual prediction.
All runtime configuration is immutable for the duration of a play attempt.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Mapping


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def percentile(values: Iterable[float], percent: float) -> float | None:
    """Linear interpolation at (n - 1) * p, including the endpoints."""
    p = _finite(percent, "percentile")
    if not 0 <= p <= 100:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(_finite(value, "percentile value") for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * p / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


@dataclass(frozen=True, slots=True)
class StartupEnvironment:
    note_speed: float
    resolution: tuple[int, int]
    fps: float
    skin: str

    def __post_init__(self) -> None:
        speed = _finite(self.note_speed, "note_speed")
        fps = _finite(self.fps, "fps")
        if speed <= 0 or fps <= 0:
            raise ValueError("note_speed and fps must be positive")
        if (not isinstance(self.resolution, tuple) or len(self.resolution) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
                       for v in self.resolution)):
            raise ValueError("resolution must be a (width, height) tuple of positive integers")
        if not isinstance(self.skin, str) or not self.skin.strip():
            raise ValueError("skin must identify the note skin")
        object.__setattr__(self, "note_speed", speed)
        object.__setattr__(self, "fps", fps)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> StartupEnvironment:
        if not isinstance(value, Mapping):
            raise ValueError("environment must be an object")
        resolution = value.get("resolution")
        if isinstance(resolution, Mapping):
            resolution = (resolution.get("width"), resolution.get("height"))
        if not isinstance(resolution, (tuple, list)):
            raise ValueError("environment requires resolution")
        skin = value.get("skin")
        if skin is None and "note_skin_type" in value:
            note_type = value["note_skin_type"]
            if isinstance(note_type, bool) or not isinstance(note_type, int) or not 1 <= note_type <= 4:
                raise ValueError("note_skin_type must be an integer in [1, 4]")
            skin = f"type{note_type}"
        return cls(_finite(value.get("note_speed"), "note_speed"), tuple(resolution),
                   _finite(value.get("fps", value.get("game_fps")), "fps"), skin)

    def to_dict(self) -> dict[str, Any]:
        return {"note_speed": float(self.note_speed), "resolution": list(self.resolution),
                "fps": float(self.fps), "skin": self.skin}

    def runtime_dict(self) -> dict[str, Any]:
        skin = re.fullmatch(r"type([1-4])", self.skin, flags=re.IGNORECASE)
        if skin is None or not float(self.fps).is_integer():
            raise ValueError("runtime artifact requires a TYPE1..4 skin and integer game_fps")
        return {"note_speed": float(self.note_speed), "resolution": list(self.resolution),
                "game_fps": int(self.fps), "note_skin_type": int(skin.group(1))}


@dataclass(frozen=True, slots=True)
class StartupCalibration:
    visual_phase_correction_ms: float = 0.0
    verified: bool = False
    environment: StartupEnvironment | None = None
    representative_count: int = 0
    corrected_p95_abs_ms: float | None = None
    evidence_source: str = "unverified-default"
    max_capture_uncertainty_ms: float | None = None

    def __post_init__(self) -> None:
        correction = _finite(self.visual_phase_correction_ms, "visual_phase_correction_ms")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be an explicit boolean")
        if self.environment is not None and not isinstance(self.environment, StartupEnvironment):
            raise ValueError("environment must be a StartupEnvironment")
        if (isinstance(self.representative_count, bool)
                or not isinstance(self.representative_count, int) or self.representative_count < 0):
            raise ValueError("representative_count must be a nonnegative integer")
        if self.corrected_p95_abs_ms is not None:
            p95 = _finite(self.corrected_p95_abs_ms, "corrected_p95_abs_ms")
            if p95 < 0:
                raise ValueError("corrected_p95_abs_ms must be nonnegative")
            object.__setattr__(self, "corrected_p95_abs_ms", p95)
        if not isinstance(self.evidence_source, str) or not self.evidence_source.strip():
            raise ValueError("evidence_source must identify calibration evidence")
        object.__setattr__(self, "visual_phase_correction_ms", correction)
        if self.max_capture_uncertainty_ms is not None:
            uncertainty = _finite(self.max_capture_uncertainty_ms, "max_capture_uncertainty_ms")
            if uncertainty < 0:
                raise ValueError("max_capture_uncertainty_ms must be nonnegative")
            object.__setattr__(self, "max_capture_uncertainty_ms", uncertainty)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> StartupCalibration:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("startup calibration must be an object")
        environment = value.get("environment")
        verified = value.get("verified", value.get("validated", False))
        if not isinstance(verified, bool):
            raise ValueError("verified must be an explicit boolean")
        if value.get("contains_synthetic_evidence") is True or value.get("synthetic") is True:
            verified = False
        if value.get("kind") == "native-start-calibration":
            ids = value.get("independent_opening_ids")
            count = value.get("sample_count", 0)
            valid_ids = (isinstance(ids, list) and len(ids) >= 10
                         and all(isinstance(item, str) and item for item in ids)
                         and len(set(ids)) == len(ids) and count == len(ids))
            if not valid_ids:
                verified = False
        return cls(
            visual_phase_correction_ms=_finite(value.get("visual_phase_correction_ms", 0),
                                               "visual_phase_correction_ms"),
            verified=verified,
            environment=(StartupEnvironment.from_mapping(environment) if environment is not None else None),
            representative_count=value.get("representative_count", value.get("sample_count", 0)),
            corrected_p95_abs_ms=value.get("corrected_p95_abs_ms", value.get("residual_p95_ms")),
            evidence_source=value.get("evidence_source", "unverified-default"),
            max_capture_uncertainty_ms=value.get("max_capture_uncertainty_ms"),
        )

    @classmethod
    def from_report(cls, report: Mapping[str, Any], *, evidence_source: str) -> StartupCalibration:
        correction = report.get("suggested_visual_phase_correction_ms")
        return cls.from_mapping({
            "visual_phase_correction_ms": 0 if correction is None else correction,
            "verified": bool(report.get("active_calibration_eligible") is True
                             and report.get("externally_verified") is True
                             and report.get("acceptance_thresholds_passed") is True
                             and report.get("contains_synthetic_evidence") is False),
            "environment": report.get("environment"),
            "representative_count": report.get("representative_count", 0),
            "corrected_p95_abs_ms": report.get("corrected_residual_ms", {}).get("p95_abs"),
            "evidence_source": evidence_source,
            "max_capture_uncertainty_ms": report.get("max_accepted_uncertainty_ms"),
        })

    def activation_reason(self, current_environment: StartupEnvironment | None, *,
                          allow_unverified: bool = False) -> str:
        if not isinstance(allow_unverified, bool):
            raise ValueError("allow_unverified must be an explicit boolean")
        reason = "verified-calibration"
        if not self.verified:
            reason = "calibration-not-externally-verified"
        elif self.representative_count < 10:
            reason = "calibration-needs-at-least-ten-representative-annotations"
        elif self.corrected_p95_abs_ms is None or self.corrected_p95_abs_ms > 20:
            reason = "calibration-corrected-p95-exceeds-20ms-or-is-unknown"
        elif abs(self.visual_phase_correction_ms) > 60:
            reason = "visual-phase-correction-exceeds-60ms"
        elif self.max_capture_uncertainty_ms is None or self.max_capture_uncertainty_ms > 20:
            reason = "calibration-capture-uncertainty-exceeds-20ms-or-is-unknown"
        elif self.environment is None or current_environment is None:
            reason = "calibration-environment-unavailable"
        elif self.environment != current_environment:
            reason = "calibration-environment-mismatch"
        if reason != "verified-calibration" and allow_unverified:
            return "experimental-unverified-calibration"
        return reason

    def active_mode_allowed(self, current_environment: StartupEnvironment | None, *,
                            allow_unverified: bool = False) -> bool:
        return self.activation_reason(current_environment, allow_unverified=allow_unverified) in {
            "verified-calibration", "experimental-unverified-calibration",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "visual_phase_correction_ms": float(self.visual_phase_correction_ms),
            "verified": self.verified,
            "environment": self.environment.to_dict() if self.environment else None,
            "representative_count": self.representative_count,
            "corrected_p95_abs_ms": self.corrected_p95_abs_ms,
            "evidence_source": self.evidence_source,
            "max_capture_uncertainty_ms": self.max_capture_uncertainty_ms,
            "time_basis": "capture-request-completion-midpoint-estimate",
            "default_correction_status": "verified" if self.verified else "UNVERIFIED",
            "profile_bias_scope": "separate; never inferred from device execution receipts",
        }


def legacy_input_target_s(threshold_crossing_s: float, *, legacy_visual_lead_ms: float = 190.0,
                          profile_advance_ms: float = 0.0, device_first_read_ms: float = 0.0) -> float:
    return _finite(_finite(threshold_crossing_s, "threshold_crossing_s")
                   + _finite(legacy_visual_lead_ms, "legacy_visual_lead_ms") / 1000
                   - _finite(profile_advance_ms, "profile_advance_ms") / 1000
                   - _finite(device_first_read_ms, "device_first_read_ms") / 1000,
                   "legacy input target")


def trajectory_input_target_s(predicted_first_due_s: float, *, visual_phase_correction_ms: float = 0.0,
                              profile_advance_ms: float = 0.0, device_first_read_ms: float = 0.0) -> float:
    """The trajectory predicts actual judgement time, so never add legacy 190 ms."""
    return _finite(_finite(predicted_first_due_s, "predicted_first_due_s")
                   + _finite(visual_phase_correction_ms, "visual_phase_correction_ms") / 1000
                   - _finite(profile_advance_ms, "profile_advance_ms") / 1000
                   - _finite(device_first_read_ms, "device_first_read_ms") / 1000,
                   "trajectory input target")


def _residual_stats(values: list[float]) -> dict[str, float | None]:
    return {"median": percentile(values, 50), "p95_abs": percentile(map(abs, values), 95),
            "max_abs": max(map(abs, values), default=None)}


def build_calibration_report(
    rows: Iterable[Mapping[str, Any]], *, expected_environment: StartupEnvironment | None = None,
    externally_verified: bool = False, min_confidence: float = .9,
    max_uncertainty_ms: float = 20.0, min_representative_count: int = 10,
    max_corrected_p95_ms: float = 20.0,
    device_execution_drifts_ms: Iterable[float] = (),
    synthetic: bool = False,
) -> dict[str, Any]:
    """Summarize independent annotations; suggest, never apply a correction.

Rows require predicted_first_due_s, annotated_first_due_s, uncertainty_ms
or estimated_uncertainty_ms, confidence, and environment. Mixed settings
    are excluded rather than pooled. Representative count is independent
    opening_id/run_id/recording_id/attempt_id values; repeated IDs are excluded.
    Rows without IDs can measure residuals but cannot validate live calibration.
    externally_verified is the caller's explicit verification of manual truth;
    evidence marked synthetic always remains ineligible for live calibration.
"""
    if not isinstance(externally_verified, bool) or not isinstance(synthetic, bool):
        raise ValueError("externally_verified must be an explicit boolean")
    if expected_environment is not None and not isinstance(expected_environment, StartupEnvironment):
        raise ValueError("expected_environment must be a StartupEnvironment")
    confidence_limit = _finite(min_confidence, "min_confidence")
    uncertainty_limit = _finite(max_uncertainty_ms, "max_uncertainty_ms")
    residual_limit = _finite(max_corrected_p95_ms, "max_corrected_p95_ms")
    if not 0 <= confidence_limit <= 1 or uncertainty_limit <= 0 or residual_limit <= 0:
        raise ValueError("invalid calibration acceptance limits")
    if (isinstance(min_representative_count, bool) or not isinstance(min_representative_count, int)
            or min_representative_count < 1):
        raise ValueError("min_representative_count must be a positive integer")
    environment = expected_environment
    residuals: list[float] = []
    uncertainties: list[float] = []
    opening_ids: set[str] = set()
    missing_opening_ids = 0
    synthetic_rows = 0
    excluded: list[dict[str, Any]] = []
    exclusion_reasons: Counter[str] = Counter()
    total = 0
    for index, row in enumerate(rows):
        if index >= 20000:
            raise ValueError("calibration exceeds the bounded 20000-row limit")
        total += 1
        reason = ""
        try:
            if not isinstance(row, Mapping):
                raise ValueError("annotation row must be an object")
            prediction = _finite(row.get("predicted_first_due_s"), "predicted_first_due_s")
            annotation = _finite(row.get("annotated_first_due_s"), "annotated_first_due_s")
            uncertainty = _finite(row.get("estimated_uncertainty_ms", row.get("uncertainty_ms")),
                                  "estimated_uncertainty_ms")
            capture_uncertainty = row.get("capture_uncertainty_ms")
            if capture_uncertainty is not None:
                capture_uncertainty = _finite(capture_uncertainty, "capture_uncertainty_ms")
                if capture_uncertainty < 0:
                    raise ValueError("capture_uncertainty_ms must be nonnegative")
                uncertainty = max(uncertainty, capture_uncertainty)
            confidence = _finite(row.get("confidence"), "confidence")
            signature = StartupEnvironment.from_mapping(row.get("environment"))
            opening_id = row.get("opening_id", row.get("run_id", row.get("recording_id", row.get("attempt_id"))))
            if opening_id is not None and (isinstance(opening_id, bool)
                    or not isinstance(opening_id, (str, int)) or opening_id == ""):
                raise ValueError("opening evidence ID must be a nonempty string or integer")
            if opening_id is not None:
                opening_id = str(opening_id)
            if not 0 <= confidence <= 1 or uncertainty < 0:
                reason = "invalid-confidence-or-uncertainty"
            elif confidence < confidence_limit:
                reason = "low-confidence"
            elif uncertainty > uncertainty_limit:
                reason = "capture-or-prediction-uncertainty-too-large"
            elif environment is not None and signature != environment:
                reason = "environment-mismatch"
            elif opening_id is not None and opening_id in opening_ids:
                reason = "duplicate-opening-evidence"
            else:
                residual = _finite((annotation - prediction) * 1000, "annotation residual")
                environment = signature
                residuals.append(residual)
                uncertainties.append(uncertainty)
                if opening_id is not None:
                    opening_ids.add(opening_id)
                else:
                    missing_opening_ids += 1
                if row.get("synthetic") is True or row.get("evidence_origin") == "synthetic":
                    synthetic_rows += 1
        except (ValueError, TypeError, OverflowError) as exc:
            reason = "invalid-or-missing-annotation-field"
            if len(excluded) < 200:
                excluded.append({"row": index, "reason": reason, "detail": str(exc)})
        else:
            if reason and len(excluded) < 200:
                excluded.append({"row": index, "reason": reason})
        if reason:
            exclusion_reasons[reason] += 1
    correction = percentile(residuals, 50)
    corrected = [value - correction for value in residuals] if correction is not None else []
    corrected_stats = _residual_stats(corrected)
    thresholds_passed = bool(
        len(opening_ids) >= min_representative_count
        and corrected_stats["p95_abs"] is not None
        and corrected_stats["p95_abs"] <= residual_limit
    )
    contains_synthetic = synthetic or synthetic_rows > 0
    live_eligible = bool(thresholds_passed and externally_verified and not contains_synthetic
                         and correction is not None and abs(correction) <= 60
                         and uncertainties and max(uncertainties) <= 20)
    # Keep transport errors independent: they do not measure game judgement.
    drifts: list[float] = []
    for index, drift in enumerate(device_execution_drifts_ms):
        if index >= 20000:
            raise ValueError("device drift exceeds the bounded 20000-value limit")
        drifts.append(_finite(drift, "device execution drift"))
    return {
        "schema_version": 1, "total_count": total, "accepted_count": len(residuals),
        "representative_count": len(opening_ids), "independent_opening_ids": sorted(map(str, opening_ids)),
        "missing_independent_opening_ids": missing_opening_ids,
        "max_accepted_uncertainty_ms": max(uncertainties, default=None),
        "contains_synthetic_evidence": contains_synthetic,
        "excluded_count": total - len(residuals), "exclusion_reasons": dict(exclusion_reasons),
        "excluded_rows": excluded, "excluded_details_truncated": total - len(residuals) > len(excluded),
        "environment": environment.to_dict() if environment else None,
        "residual_definition": "(annotated game first judgement - predicted first judgement) * 1000",
        "residual_ms": _residual_stats(residuals),
        "suggested_visual_phase_correction_ms": correction,
        "corrected_residual_ms": corrected_stats,
        "acceptance_thresholds": {"min_confidence": confidence_limit,
                                  "max_uncertainty_ms": uncertainty_limit,
                                  "min_representative_count": min_representative_count,
                                  "max_corrected_p95_ms": residual_limit},
        "acceptance_thresholds_passed": thresholds_passed,
        "externally_verified": externally_verified,
        "active_calibration_eligible": live_eligible,
        "calibration_status": ("synthetic-evidence-not-live-calibration" if contains_synthetic
                               else "externally-verified-candidate" if live_eligible
                               else "UNVERIFIED" if not externally_verified else "insufficient-evidence"),
        "device_execution_drift_ms": {"count": len(drifts), **_residual_stats(drifts),
                                      "scope": "transport only; not game judgement truth"},
        "time_basis": "capture-request-completion-midpoint-estimate; independent annotation required",
        "profile_update": "none; suggested correction replaces visual correction and does not stack Profile bias",
    }


def build_calibration_artifact(report: Mapping[str, Any], *, evidence_source: str) -> dict[str, Any]:
    """Export the runtime schema; unverified/synthetic reports stay invalid.

Independent opening IDs are mandatory for live activation. The export does
not create, install or enable a profile, and never converts transport drift
or synthetic regression measurements into externally verified game truth.
"""
    calibration = StartupCalibration.from_report(report, evidence_source=evidence_source)
    environment = calibration.environment
    runtime_environment: dict[str, Any] | None = None
    export_error: str | None = None
    if environment is not None:
        try:
            runtime_environment = environment.runtime_dict()
        except ValueError as exc:
            export_error = str(exc)
    validated = bool(runtime_environment is not None and calibration.active_mode_allowed(environment))
    return {
        "schema_version": 1, "kind": "native-start-calibration", "validated": validated,
        "environment": runtime_environment, "sample_count": calibration.representative_count,
        "residual_p95_ms": calibration.corrected_p95_abs_ms,
        "visual_phase_correction_ms": calibration.visual_phase_correction_ms,
        "max_capture_uncertainty_ms": calibration.max_capture_uncertainty_ms,
        "evidence_source": evidence_source,
        "independent_opening_ids": report.get("independent_opening_ids", []),
        "contains_synthetic_evidence": report.get("contains_synthetic_evidence", False),
        "raw_error_report": report.get("residual_ms"),
        "activation_reason": export_error or calibration.activation_reason(environment),
        "time_basis": "capture-request-completion-midpoint-estimate; manual game first-due annotations",
        "compensation_scope": "visual phase only; Profile advance and device first-read remain separate",
    }
