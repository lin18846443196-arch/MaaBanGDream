"""Freeze and validate native startup rollout options before observing notes."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NativeStartSyncOptions:
    mode: str = "shadow"
    visual_phase_correction_ms: float = 0.0
    calibration_verified: bool = False
    calibration_path: str | None = None
    calibration_samples: int = 0
    calibration_residual_p95_ms: float | None = None

    def to_mapping(self) -> dict[str, object]:
        return {"mode": self.mode,
                "visual_phase_correction_ms": self.visual_phase_correction_ms,
                "calibration_verified": self.calibration_verified,
                "calibration_path": self.calibration_path,
                "calibration_samples": self.calibration_samples,
                "calibration_residual_p95_ms": self.calibration_residual_p95_ms}


def validate_start_sync_mode(value: object) -> str:
    if not isinstance(value, str) or value not in {"off", "shadow", "active"}:
        raise ValueError("native_start_sync_mode 必须是 off/shadow/active")
    return value


def resolve_start_sync_options(options: dict, *, run_mode: str,
                               profile_root: Path, environment: dict) -> NativeStartSyncOptions:
    mode = validate_start_sync_mode(options.get("native_start_sync_mode", "shadow"))
    # The rollout is restricted to cooperative. Other modes keep their gate.
    if str(run_mode).strip().lower() != "cooperative":
        return NativeStartSyncOptions(mode="off")
    if mode != "active":
        return NativeStartSyncOptions(mode=mode)
    name = options.get("native_start_calibration_file", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Native 首组接管缺少标注回放校准文件，先使用 shadow")
    root = Path(profile_root).resolve()
    path = (root / name).resolve()
    if path == root or not path.is_relative_to(root) or Path(name).is_absolute():
        raise ValueError("Native 首组校准文件必须位于 profiles 目录内")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "native-start-calibration":
        raise ValueError("Native 首组校准文件格式无效")
    if data.get("validated") is not True:
        raise ValueError("Native 首组校准未通过标注精度验证")
    recorded = data.get("environment")
    if not isinstance(recorded, dict):
        raise ValueError("Native 首组校准缺少环境信息")
    for key in ("resolution", "game_fps", "note_speed", "note_skin_type"):
        expected = environment.get(key)
        if expected is None or recorded.get(key) != expected:
            raise ValueError(f"Native 首组校准环境不匹配：{key}")
    count = data.get("sample_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 10:
        raise ValueError("Native 首组校准至少需要 10 个有效标注开场")
    correction = data.get("visual_phase_correction_ms")
    p95 = data.get("residual_p95_ms")
    for value in (correction, p95):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Native 首组校准时延必须是有限数字")
    if abs(correction) > 60 or not 0 <= p95 <= 20:
        raise ValueError("Native 首组校准超出允许误差或修正范围")
    from .startup_calibration import StartupCalibration, StartupEnvironment
    calibration = StartupCalibration.from_mapping(data)
    if not calibration.active_mode_allowed(StartupEnvironment.from_mapping(environment)):
        raise ValueError("Native 首组校准不满足接管条件：" +
                         calibration.activation_reason(StartupEnvironment.from_mapping(environment)))
    return NativeStartSyncOptions(mode="active", visual_phase_correction_ms=float(correction),
        calibration_verified=True, calibration_path=str(path), calibration_samples=count,
        calibration_residual_p95_ms=float(p95))
