"""Bounded offline first-group replay. This tool never connects to a device.

Dense JSON/JSONL manifests must retain capture request/completion intervals.
Historical screenshots/video without those intervals remain useful visual
evidence, but are reported as insufficient for first-note timing validation.
An encoded video FPS is never interpreted as a game capture timestamp.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
from itertools import islice
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent"))

MAX_ROWS = 20000
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def read_rows(path: Path, *, max_rows: int = MAX_ROWS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    metadata: dict[str, Any] = {}
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line in text.splitlines():
            if line.strip():
                if len(rows) >= max_rows:
                    raise ValueError(f"input exceeds {max_rows} rows")
                rows.append(json.loads(line))
    else:
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            metadata = {k: v for k, v in payload.items() if k not in {"frames", "annotations", "rows"}}
            rows = payload.get("frames", payload.get("annotations", payload.get("rows")))
        else:
            rows = payload
        if not isinstance(rows, list):
            raise ValueError("JSON input must be a row array or an object containing frames/annotations/rows")
        if len(rows) > max_rows:
            raise ValueError(f"input exceeds {max_rows} rows")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("input rows must be JSON objects")
    return rows, metadata


@dataclass(frozen=True, slots=True)
class ReplaySample:
    image: Any
    capture_id: str | int
    request_started_at: float
    completed_at: float
    consumed_at: float
    is_new: bool = True
    source: str = "offline-explicit-manifest"

    @property
    def captured_at(self) -> float:
        return self.request_started_at + (self.completed_at - self.request_started_at) / 2

    @property
    def uncertainty_ms(self) -> float:
        return (self.completed_at - self.request_started_at) * 500

    @property
    def age_ms(self) -> float:
        return (self.consumed_at - self.captured_at) * 1000

    @property
    def eligible_for_start(self) -> bool:
        return self.is_new and 0 <= self.age_ms <= 80 and self.uncertainty_ms <= 40

    def diagnostics(self) -> dict[str, Any]:
        return {"capture_id": self.capture_id, "request_started_at": self.request_started_at,
                "completed_at": self.completed_at, "consumed_at": self.consumed_at,
                "captured_at": self.captured_at, "uncertainty_ms": self.uncertainty_ms,
                "age_ms": self.age_ms, "is_new": self.is_new,
                "eligible_for_start": self.eligible_for_start, "source": self.source}


@dataclass(frozen=True, slots=True)
class ManifestFrame:
    image: Path
    capture_id: str | int
    request_started_at: float
    completed_at: float
    consumed_at: float
    is_new: bool
    source: str
    annotated_first_due_s: float | None = None


def _capture_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("frame_sample", "capture_sample", "capture"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            result.update(nested)
    # Current diagnostic streams can place the frame provenance in events.
    diagnostics = row.get("diagnostics", [])
    if isinstance(diagnostics, list):
        for event in diagnostics[:100]:
            if not isinstance(event, Mapping):
                continue
            if event.get("event") in {"frame_sample", "capture_sample"}:
                result.update(event)
            for key in ("frame_sample", "capture_sample"):
                if isinstance(event.get(key), Mapping):
                    result.update(event[key])
    return result


def parse_manifest(rows: list[dict[str, Any]], base_dir: Path, *,
                   metadata: Mapping[str, Any] | None = None) -> tuple[list[ManifestFrame], dict[str, Any]]:
    frames: list[ManifestFrame] = []
    excluded: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    metadata = metadata or {}
    for index, row in enumerate(rows):
        reason = ""
        try:
            fields = _capture_fields(row)
            image = row.get("image", row.get("path", row.get("file", row.get("screenshot"))))
            if not isinstance(image, str) or not image:
                reason = "no-retained-image-reference"
                raise ValueError("frame has no image/path/file/screenshot reference")
            image_path = Path(image)
            if not image_path.is_absolute():
                image_path = base_dir / image_path
            if not image_path.is_file():
                reason = "retained-image-not-found"
                raise ValueError(f"image does not exist: {image}")
            timing_keys = ("request_started_at", "completed_at", "consumed_at", "capture_id", "is_new")
            if any(key not in fields for key in timing_keys):
                reason = "capture-request-interval-or-freshness-missing"
                raise ValueError("host timestamp/encoded FPS cannot replace request/completion intervals and freshness")
            request = finite(fields["request_started_at"], "request_started_at")
            completed = finite(fields["completed_at"], "completed_at")
            consumed = finite(fields["consumed_at"], "consumed_at")
            if request > completed or completed > consumed:
                reason = "invalid-capture-interval"
                raise ValueError("capture timing must satisfy request <= completed <= consumed")
            capture_id = fields["capture_id"]
            if isinstance(capture_id, bool) or not isinstance(capture_id, (str, int)):
                raise ValueError("capture_id must be a string or integer")
            if not isinstance(fields["is_new"], bool):
                raise ValueError("is_new must be an explicit boolean")
            annotation = row.get("annotated_first_due_s", metadata.get("annotated_first_due_s"))
            frames.append(ManifestFrame(image_path, capture_id, request, completed, consumed,
                                        fields["is_new"], str(fields.get("source", "offline-explicit-manifest")),
                                        finite(annotation, "annotated_first_due_s") if annotation is not None else None))
        except (ValueError, TypeError, OverflowError) as exc:
            reason = reason or "invalid-frame-field"
            reasons[reason] = reasons.get(reason, 0) + 1
            if len(excluded) < 100:
                excluded.append({"row": index, "reason": reason, "detail": str(exc)})
    return frames, {"input_rows": len(rows), "usable_rows": len(frames),
                    "excluded_rows": len(rows) - len(frames), "exclusion_reasons": reasons,
                    "exclusions": excluded, "exclusion_details_truncated": len(rows) - len(frames) > len(excluded)}


def load_recording(recording: Path, *, max_rows: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not recording.is_dir():
        raise ValueError("recording must be a directory")
    # The bounded inventory establishes schemas without recursive scanning.
    inventory = sorted(path.name for path in islice(recording.iterdir(), 100))
    startup_path = recording / "native-startup.jsonl"
    if startup_path.is_file():
        startup_rows, _ = read_rows(startup_path, max_rows=max_rows)
        if startup_rows:
            return startup_rows, {
                "source": "recording-native-startup-manifest", "inventory": inventory,
                "native_startup_manifest": startup_path.name,
                "native_startup_rows": len(startup_rows),
                "evidence_limitations": [
                    "bounded startup ring may lack the clean prelude or complete first-group trajectory",
                    "host request/completion midpoint remains an estimate of game capture time",
                    "timing accuracy needs independent manual game judgement annotations",
                ],
            }
    trace: list[dict[str, Any]] = []
    trace_path = recording / "trace.jsonl"
    if trace_path.is_file():
        trace, _ = read_rows(trace_path, max_rows=max_rows)
    by_frame = {row.get("frame"): row for row in trace if "frame" in row}
    retained: list[dict[str, Any]] = []
    for name in ("events.jsonl", "checkpoints.jsonl"):
        path = recording / name
        if not path.is_file():
            continue
        entries, _ = read_rows(path, max_rows=max_rows)
        for event in entries:
            if isinstance(event.get("screenshot"), str):
                merged = dict(by_frame.get(event.get("frame"), {}))
                merged.update(event)
                retained.append(merged)
                if len(retained) > max_rows:
                    raise ValueError("retained screenshots exceed bounded row limit")
    # New schemas may retain direct screenshots on trace rows.
    retained.extend(row for row in trace if any(isinstance(row.get(k), str)
                    for k in ("image", "path", "file", "screenshot")))
    if len(retained) > max_rows:
        raise ValueError("retained screenshots exceed bounded row limit")
    sidecar = recording / "video_frames.jsonl"
    sidecar_rows: list[dict[str, Any]] = []
    if sidecar.is_file():
        sidecar_rows, _ = read_rows(sidecar, max_rows=max_rows)
    sidecar_interval_rows = sum(all(key in _capture_fields(row)
                                  for key in ("request_started_at", "completed_at", "capture_id", "is_new"))
                                for row in sidecar_rows)
    return retained, {
        "source": "recording-retained-screenshots", "inventory": inventory,
        "trace_rows": len(trace), "retained_screenshot_rows": len(retained),
        "video_sidecar_rows": len(sidecar_rows), "video_sidecar_interval_rows": sidecar_interval_rows,
        "video_decode": "not-used; explicit image manifest with retained request intervals required",
        "evidence_limitations": [
            "sparse retained screenshots cannot establish a dense first-group trajectory",
            "encoded video FPS is not a capture clock",
            "host trace timestamps without request/completion intervals have unknown acquisition uncertainty",
        ],
    }


def _decode_image(path: Path) -> Any:
    from realtime.vision_io import imread_unicode
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("image exceeds bounded byte limit")
    image = imread_unicode(path)
    if image is None or image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("image could not be decoded as BGR colour")
    if image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
        raise ValueError("image exceeds bounded pixel limit")
    return image


def replay_variant(chart: Any, frames: list[ManifestFrame], *, name: str = "baseline",
                   repeat_every: int = 0, drop_every: int = 0, stall_ms: int = 0,
                   stall_every: int = 10, mark_started: bool = False,
                   synchronizer_factory: Any = None, image_loader: Any = None) -> dict[str, Any]:
    if synchronizer_factory is None:
        from realtime.first_group_sync import FirstGroupSynchronizer
        synchronizer_factory = FirstGroupSynchronizer
    synchronizer = synchronizer_factory(chart)
    image_loader = image_loader or _decode_image
    prediction: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []
    observed = dropped = repeats = stalls = error_count = 0
    age_max = uncertainty_max = 0.0
    shift_s = 0.0
    fresh_times: list[float] = []
    seen_ids: set[str | int] = set()
    for index, frame in enumerate(frames, 1):
        if drop_every and index % drop_every == 0:
            dropped += 1
            continue
        try:
            image = image_loader(frame.image)
            sample = ReplaySample(image, frame.capture_id, frame.request_started_at + shift_s,
                                  frame.completed_at + shift_s, frame.consumed_at + shift_s,
                                  frame.is_new, frame.source)
            if stall_ms and index % stall_every == 0:
                # Acquisition stalls widen uncertainty and delay consumption;
                # following captures also have a cadence hole. No new render
                # timestamp is invented for the stalled image.
                duration_s = stall_ms / 1000
                sample = replace(sample, completed_at=sample.completed_at + duration_s,
                                 consumed_at=sample.consumed_at + duration_s)
                shift_s += duration_s
                stalls += 1
            age_max = max(age_max, sample.age_ms)
            uncertainty_max = max(uncertainty_max, sample.uncertainty_ms)
            if sample.eligible_for_start and sample.capture_id not in seen_ids:
                fresh_times.append(sample.captured_at)
                seen_ids.add(sample.capture_id)
            result = synchronizer.observe(sample)
            observed += 1
            if result is not None and prediction is None:
                prediction = {"first_due_s": float(result.first_due_s),
                              "uncertainty_ms": float(result.uncertainty_ms),
                              "confidence": float(result.confidence),
                              "capture_id": sample.capture_id, "captured_at": sample.captured_at}
                if frame.annotated_first_due_s is not None:
                    # Injected cadence delays preserve game truth rather than
                    # shifting it to make a late prediction look accurate.
                    prediction["annotated_first_due_s"] = frame.annotated_first_due_s
                    prediction["error_ms"] = (result.first_due_s - frame.annotated_first_due_s) * 1000
                if mark_started:
                    synchronizer.mark_started()
            if repeat_every and index % repeat_every == 0:
                cached = replace(sample, is_new=False, consumed_at=sample.consumed_at + .001,
                                 source="offline-injected-cached-frame")
                synchronizer.observe(cached)
                observed += 1
                repeats += 1
        except (ValueError, RuntimeError, OSError, TypeError) as exc:
            error_count += 1
            if len(errors) < 100:
                errors.append({"row": index - 1, "error": str(exc)})
    report = synchronizer.report()
    gaps_ms = [(b - a) * 1000 for a, b in zip(fresh_times, fresh_times[1:]) if b > a]
    dense = bool(len(fresh_times) >= 6 and gaps_ms and max(gaps_ms) <= 100.0 + 1e-6
                 and statistics.median(gaps_ms) <= 50.0 + 1e-6)
    status = ("predicted-with-independent-annotation" if prediction and "error_ms" in prediction
              else "predicted-unannotated" if prediction else "rejected" if report.get("rejected_reason")
              else "insufficient-evidence")
    return {"name": name, "status": status, "prediction": prediction,
            "observed_samples": observed, "injected_cached_samples": repeats,
            "dropped_samples": dropped, "injected_stalls": stalls,
            "max_age_ms": age_max, "max_capture_uncertainty_ms": uncertainty_max,
            "eligible_distinct_frames": len(fresh_times),
            "max_eligible_capture_gap_ms": max(gaps_ms, default=None),
            "dense_cadence_evidence": dense, "errors": errors, "error_count": error_count,
            "error_details_truncated": error_count > len(errors), "tracker": report,
            "input_authority": "offline only; no device actions",
            "validation_limit": ("independent annotation present" if prediction and "error_ms" in prediction
                                 else "no independent game judgement annotation; accuracy unverified")}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.calibration:
        from realtime.startup_calibration import StartupEnvironment, build_calibration_artifact, build_calibration_report
        rows, metadata = read_rows(Path(args.calibration), max_rows=args.max_frames)
        environment = metadata.get("environment")
        report = build_calibration_report(
            rows, expected_environment=StartupEnvironment.from_mapping(environment) if environment else None,
            externally_verified=metadata.get("externally_verified", False),
            device_execution_drifts_ms=metadata.get("device_execution_drifts_ms", []),
            synthetic=metadata.get("synthetic", False))
        result = {"mode": "calibration-annotations", "report": report}
        if args.calibration_artifact_output:
            result["artifact"] = build_calibration_artifact(report, evidence_source=str(Path(args.calibration).resolve()))
        return result
    if not args.chart or not (args.frames or args.recording):
        raise ValueError("replay requires --chart and either --frames or --recording")
    from realtime.chart_timeline import ChartTimeline
    chart = ChartTimeline.from_json(args.chart)
    if args.frames:
        path = Path(args.frames).resolve()
        rows, metadata = read_rows(path, max_rows=args.max_frames)
        base_dir = path.parent
        evidence = {"source": "explicit-frame-manifest"}
    else:
        base_dir = Path(args.recording).resolve()
        rows, evidence = load_recording(base_dir, max_rows=args.max_frames)
        metadata = {}
    frames, manifest_report = parse_manifest(rows, base_dir, metadata=metadata)
    variants: list[dict[str, Any]] = []
    if frames:
        variants.append(replay_variant(chart, frames, mark_started=args.mark_started))
        specifications: list[dict[str, int]] = []
        if args.repeat_every:
            specifications.append({"repeat_every": args.repeat_every})
        if args.drop_every:
            specifications.append({"drop_every": args.drop_every})
        if args.stall_ms:
            specifications.append({"stall_ms": args.stall_ms, "stall_every": args.stall_every})
        if len(specifications) > 1:
            specifications.append({"repeat_every": args.repeat_every, "drop_every": args.drop_every,
                                   "stall_ms": args.stall_ms, "stall_every": args.stall_every})
        for spec in specifications:
            name = ",".join(f"{key}={value}" for key, value in spec.items() if value)
            variants.append(replay_variant(chart, frames, name=name, mark_started=args.mark_started, **spec))
    return {"schema_version": 1, "mode": "offline-replay",
            "status": "replayed" if frames else "insufficient-evidence",
            "chart": str(Path(args.chart).resolve()), "manifest": manifest_report,
            "evidence": evidence, "variants": variants,
            "time_basis": "retained host request/completion midpoint estimate, never encoded FPS",
            "calibration_scope": "no automatic changes to legacy190, Profile or device compensation"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--chart")
    inputs = result.add_mutually_exclusive_group()
    inputs.add_argument("--frames", help="dense JSON/JSONL image and capture-provenance manifest")
    inputs.add_argument("--recording", help="recording directory with retained screenshot references")
    inputs.add_argument("--calibration", help="standalone annotation JSON/JSONL report")
    result.add_argument("--repeat-every", type=int, default=0)
    result.add_argument("--drop-every", type=int, default=0)
    result.add_argument("--stall-ms", type=int, choices=(50, 100, 200, 400), default=0)
    result.add_argument("--stall-every", type=int, default=10)
    result.add_argument("--max-frames", type=int, default=MAX_ROWS)
    result.add_argument("--mark-started", action="store_true")
    result.add_argument("--output", help="optional JSON report file")
    result.add_argument("--calibration-artifact-output", help="explicit runtime calibration artifact export; requires --calibration")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if not 1 <= arguments.max_frames <= MAX_ROWS:
            raise ValueError(f"--max-frames must be in [1, {MAX_ROWS}]")
        if arguments.repeat_every < 0 or arguments.drop_every < 0 or arguments.stall_every < 1:
            raise ValueError("repeat/drop must be nonnegative; stall-every must be positive")
        if arguments.calibration_artifact_output and not arguments.calibration:
            raise ValueError("--calibration-artifact-output requires --calibration")
        report = run(arguments)
        if arguments.calibration_artifact_output:
            Path(arguments.calibration_artifact_output).write_text(
                json.dumps(report["artifact"], ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        code = 0
    except (ValueError, OSError, json.JSONDecodeError, ImportError) as exc:
        report = {"schema_version": 1, "status": "invalid-input", "error": str(exc)}
        code = 2
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    if arguments.output:
        Path(arguments.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
