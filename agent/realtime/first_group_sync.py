"""Chart constrained, input-free first-group synchronization for multiplayer.

Capture midpoint is an estimate, not a game presentation timestamp.  A
prediction therefore carries its uncertainty; callers must check their own
input bias/transport deadline before allowing any touch publication.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any

import cv2
import numpy as np

from .chart_timeline import ChartTimeline
from .frame_sample import FrameSample
from .life_monitor import LifeDetector
from .note_detector import NoteDetector, NoteKind
from .playfield_monitor import PlayfieldDetector
from .prepare_popup import CooperativePreparePopupDetector


@dataclass(frozen=True, slots=True)
class FirstGroupSyncConfig:
    ready_frames: int = 3
    ready_span_ms: float = 50.0
    max_frame_gap_ms: float = 100.0
    max_age_ms: float = 80.0
    max_capture_uncertainty_ms: float = 40.0
    min_track_frames: int = 3
    min_travel_px: float = 32.0
    min_head_y: float = 300.0
    max_initial_head_y: float = 480.0
    prediction_head_y: float = 470.0
    max_head_y: float = 555.0
    judgement_y: float = 590.0
    min_send_lead_ms: float = 30.0
    max_prediction_lead_ms: float = 280.0
    max_uncertainty_ms: float = 20.0
    chord_agreement_ms: float = 20.0
    prefix_window_s: float = 2.0
    prefix_groups: int = 6
    head_group_y_tolerance: float = 22.0
    # Changes to these ROIs see UI fade, not moving notes/stage spotlights.
    roi_change_threshold: float = 16.0
    whole_frame_change_threshold: float = 65.0

    def __post_init__(self) -> None:
        if self.ready_frames < 3 or self.min_track_frames < 3:
            raise ValueError("first-group synchronization needs at least three fresh frames")
        if not (0 < self.min_head_y < self.max_initial_head_y <= self.max_head_y < self.judgement_y):
            raise ValueError("invalid first-group geometry window")
        positive = (
            self.ready_span_ms, self.max_frame_gap_ms, self.max_age_ms,
            self.max_capture_uncertainty_ms, self.min_travel_px,
            self.min_send_lead_ms, self.max_prediction_lead_ms,
            self.max_uncertainty_ms, self.chord_agreement_ms,
        )
        if not all(math.isfinite(v) and v > 0 for v in positive):
            raise ValueError("invalid first-group timing limits")


@dataclass(frozen=True, slots=True)
class FirstGroupPrediction:
    first_due_s: float
    uncertainty_ms: float
    confidence: float
    captured_at: float
    first_chart_time_s: float
    lanes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ObservedHead:
    """A playable note head in canonical 1280 x 720 coordinates."""

    lane: int
    kind: str
    y: float
    x: float = 0.0
    width: float = 0.0
    is_head: bool = True


@dataclass(frozen=True, slots=True)
class _ExpectedHead:
    lane: int
    kind: str
    time_s: float
    note_index: int
    direction: str | None = None
    width: int = 1


def _signature(group: tuple[_ExpectedHead, ...]) -> tuple[tuple[int, str], ...]:
    return tuple(sorted((head.lane, head.kind) for head in group))


def _head_prefix(chart: ChartTimeline, config: FirstGroupSyncConfig) -> tuple[tuple[_ExpectedHead, ...], ...]:
    """Use playable starts; never turn hold tails/checkpoints into starts."""
    paths = {path.note_index: path for path in chart.hold_paths}
    heads: list[_ExpectedHead] = []
    for event in chart.judgements:
        if event.kind == "tap":
            kind = "flick" if event.flick else "tap"
        elif event.kind == "hold-head":
            path = paths.get(event.note_index)
            if path is None or abs(path.head.time_s - event.time_s) > 1e-5:
                continue
            if abs(float(path.head.lane) - event.lane) > .51:
                continue
            kind = "hold"
        else:
            continue
        if not (math.isfinite(event.time_s) and 0 <= event.lane < 7):
            continue
        heads.append(_ExpectedHead(event.lane, kind, event.time_s,
                                   event.note_index, event.direction,
                                   event.directional_width))
    heads.sort(key=lambda head: (head.time_s, head.lane))
    groups: list[list[_ExpectedHead]] = []
    for head in heads:
        if groups and head.time_s - groups[0][0].time_s > config.prefix_window_s:
            break
        if not groups or abs(head.time_s - groups[-1][0].time_s) > 1e-5:
            if len(groups) >= config.prefix_groups:
                break
            groups.append([])
        groups[-1].append(head)
    return tuple(tuple(group) for group in groups)


class _StartupNoteDetector(NoteDetector):
    # Only the local trajectory window participates in startup prediction.
    # The ordinary detector already downsamples this ROI by two internally.
    PLAYFIELD_TOP = 270
    PLAYFIELD_BOTTOM = 566


class FirstGroupHeadDetector:
    """Adapt the existing detector while excluding slide ribbon checkpoints.

TYPE1 holds require a visible white head rim at the body's observed head.
Thin green checkpoint bars have no rim and cannot start a chart.  TYPE4's
compact solid heads and actual flick chevrons retain their own geometry.
"""

    def __init__(self) -> None:
        self._detector = _StartupNoteDetector()

    def detect(self, image: np.ndarray, timestamp: float) -> list[ObservedHead]:
        detected = self._detector.detect(image, timestamp)
        result: list[ObservedHead] = []
        for note in detected:
            kind = ("hold" if note.kind == NoteKind.HOLD else "flick"
                    if note.kind == NoteKind.FLICK else "tap")
            y = note.y + note.height / 2 if kind == "hold" else note.y
            if not 270 <= y < 566:
                continue
            if kind == "hold" and note.hold_body_confidence < .5:
                continue
            # Confirm a rim around this particular head, rather than any
            # bright component elsewhere in the whole timing band.
            left = max(0, int(note.x - max(35, note.width * .65)))
            right = min(1280, int(note.x + max(35, note.width * .65)))
            top = max(0, int(y - 25))
            bottom = min(720, int(y + 25))
            hsv = cv2.cvtColor(image[top:bottom, left:right, :3], cv2.COLOR_BGR2HSV)
            rim = cv2.inRange(hsv, (0, 0, 205), (179, 100, 255))
            rim = cv2.morphologyEx(rim, cv2.MORPH_OPEN, np.ones((1, 11), np.uint8))
            _, _, stats, _ = cv2.connectedComponentsWithStats(rim)
            rim_visible = any(w >= 27 and w / max(h, 1) >= 2.5 and area >= 40
                              for _, _, w, h, area in stats[1:])
            # A filled cyan/yellow TYPE4 diamond needs no white rim. Holds
            # still need rim/body evidence; a green bar is never enough.
            compact_solid = (kind == "tap" and 18 <= note.width <= 85
                             and 8 <= note.height <= 42
                             and 1.0 <= note.width / max(note.height, 1) <= 4.0)
            chevron = kind == "flick" and note.height >= 6 and note.width >= 25
            if not (rim_visible or compact_solid or chevron):
                continue
            candidate = ObservedHead(note.lane, kind, float(y), note.x, note.width)
            # Ordinary coloured rings can sit below a flick chevron. Keep
            # the typed flick/hold at that position rather than two starts.
            overlap = next((i for i, old in enumerate(result)
                            if old.lane == candidate.lane and abs(old.y - candidate.y) <= 18), None)
            if overlap is None:
                result.append(candidate)
            elif candidate.kind in {"hold", "flick"} and result[overlap].kind == "tap":
                result[overlap] = candidate
        # Pale TYPE1 skill/cyan heads can fall outside the saturated colour
        # ranges. Recover their white ring, remove the thin white chord link,
        # and classify the colour *inside that ring*. This also keeps a wide
        # hold ribbon from hiding the second head of a chord.
        hsv = cv2.cvtColor(image[270:566, :, :3], cv2.COLOR_BGR2HSV)
        rim = cv2.inRange(hsv, (0, 0, 210), (179, 90, 255))
        rim = cv2.morphologyEx(rim, cv2.MORPH_OPEN, np.ones((2, 3), np.uint8))
        rim = cv2.morphologyEx(rim, cv2.MORPH_OPEN, np.ones((1, 9), np.uint8))
        _, _, stats, centroids = cv2.connectedComponentsWithStats(rim)
        parts: list[tuple[int, float, float, int]] = []
        for (x, y, width, height, area), (cx, cy) in zip(stats[1:], centroids[1:]):
            cy += 270
            if not (25 <= width <= 220 and 2 <= height <= 35
                    and width / height >= 2.2 and area >= 40):
                continue
            centers = self._detector.centers_at(cy)
            spacing = max(24, centers[1] - centers[0])
            lane = min(range(7), key=lambda index: abs(cx - centers[index]))
            if abs(cx - centers[lane]) > spacing * .42 or width > spacing * 1.5:
                continue
            local = hsv[max(0, y-12):min(hsv.shape[0], y+height+12),
                        max(0, x-5):min(1280, x+width+5)]
            saturated = (local[:, :, 1] >= 65) & (local[:, :, 2] >= 150)
            if int(saturated.sum()) < 35:
                continue
            parts.append((lane, float(cx), float(cy), int(width)))
        clusters: list[list[tuple[int, float, float, int]]] = []
        for part in sorted(parts, key=lambda item: (item[0], item[2])):
            if (clusters and clusters[-1][0][0] == part[0]
                    and part[2] - clusters[-1][0][2] <= 50):
                clusters[-1].append(part)
            else:
                clusters.append([part])
        for cluster in clusters:
            lane = cluster[0][0]
            cx = float(np.mean([part[1] for part in cluster]))
            cy = float((min(part[2] for part in cluster) + max(part[2] for part in cluster)) / 2)
            width = max(part[3] for part in cluster)
            local = hsv[max(0, round(cy-270-12)):min(hsv.shape[0], round(cy-270+13)),
                        max(0, round(cx-width*.5)):min(1280, round(cx+width*.5))]
            green = cv2.inRange(local, (38, 55, 130), (81, 255, 255))
            cyan_or_gold = (cv2.inRange(local, (82, 55, 130), (110, 255, 255))
                            | cv2.inRange(local, (10, 55, 130), (37, 255, 255)))
            known_hold = any(note.lane == lane and note.kind == NoteKind.HOLD
                             and abs(note.y + note.height / 2 - cy) < 22
                             and (note.hold_body_confidence >= .5 or note.height >= 40)
                             for note in detected)
            kind = "hold" if known_hold or int(green.sum()) > int(cyan_or_gold.sum()) * 1.5 else "tap"
            candidate = ObservedHead(lane, kind, cy, cx, width)
            overlap = next((index for index, old in enumerate(result)
                            if old.lane == lane and abs(old.y-cy) <= 35), None)
            if overlap is None:
                result.append(candidate)
            elif result[overlap].kind == kind:
                # The centre of the upper/lower white arcs is the actual
                # visual head centre, unlike the whole-body green centroid.
                result = [old for old in result
                          if not (old.lane == lane and old.kind == kind and abs(old.y-cy) <= 35)]
                result.append(candidate)
        return result


class FirstGroupSynchronizer:
    """Input-free sidecar; one prediction and no implicit fallback/rebase."""

    def __init__(self, chart: ChartTimeline, *, detector: Any = None,
                 playfield_detector: Any = None, popup_detector: Any = None,
                 config: FirstGroupSyncConfig | None = None,
                 life_detector: Any = None) -> None:
        self.config = config or FirstGroupSyncConfig()
        self.prefix = _head_prefix(chart, self.config)
        self._detector = detector or FirstGroupHeadDetector()
        self._playfield = playfield_detector or PlayfieldDetector()
        self._popup = popup_detector or CooperativePreparePopupDetector(verify_content=True)
        self._life = life_detector or LifeDetector()
        self.state = "WaitingPlayfield"
        self.reason = "awaiting-playfield"
        self.rejected_reason: str | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=96)
        self._max_capture_id: int | None = None
        self._last_frame_s: float | None = None
        self._last_image: np.ndarray | None = None
        self._ready_since: float | None = None
        self._ready_count = 0
        self._clean_seen = False
        self._tracks: dict[tuple[int, str], deque[tuple[float, float, float]]] = {}
        self._head_history: deque[tuple[float, list[ObservedHead]]] = deque(maxlen=8)
        self._mismatch: tuple[float, float, tuple[tuple[int, str], ...]] | None = None
        self._front_y: float | None = None
        self._front_signature: tuple[tuple[int, str], ...] | None = None
        self._first_candidate_seen: float | None = None
        self._prediction: FirstGroupPrediction | None = None
        self._life_peak: int | None = None
        self._life_drop_streak = 0
        self._next_life_check = float("-inf")
        self.fresh_frames = 0
        self.ignored_frames = 0
        self.member_wait_frames = 0
        self.gap_events = 0
        self.candidate: dict[str, Any] | None = None
        if not self.prefix:
            self._reject("chart-has-no-confirmed-playable-heads", None)
        elif len(set(_signature(self.prefix[0]))) != len(self.prefix[0]):
            self._reject("ambiguous-overlapping-first-heads", None)
        elif any(head.direction is not None or head.width != 1 for head in self.prefix[0]):
            # The current visual extractor reports flick type and lane, but
            # cannot verify horizontal direction or directional-flick width.
            # Keeping this metadata in a report is not proof of a match.
            self._reject("unsupported-first-head-direction-or-width", None)

    def _event(self, event: str, at: float | None, **detail: Any) -> None:
        self._events.append({"event": event, "captured_at": at, **detail})

    def _state(self, state: str, reason: str, at: float) -> None:
        if state != self.state or reason != self.reason:
            self._event("state", at, state=state, reason=reason)
        self.state, self.reason = state, reason

    def _reset(self) -> None:
        self._ready_since = None
        self._ready_count = 0
        self._clean_seen = False
        self._tracks.clear()
        self._head_history.clear()
        self._mismatch = None
        self._front_y = None
        self._front_signature = None
        self._first_candidate_seen = None
        self.candidate = None
        self._last_image = None

    def _reject(self, reason: str, at: float | None) -> None:
        self.rejected_reason = reason
        self.reason = reason
        self._event("rejected", at, reason=reason)

    def mark_started(self) -> None:
        """Called only by the Native owner after it accepts the deadline."""
        if self._prediction is None or self.rejected_reason:
            raise RuntimeError("first-group clock has not been safely predicted")
        self._state("Playing", "native-owner-started", self._prediction.captured_at)

    @staticmethod
    def _canonical(image: Any) -> np.ndarray:
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
            raise ValueError("first-group synchronizer requires HxWx3 image")
        if image.shape[:2] != (720, 1280):
            return cv2.resize(image[:, :, :3], (1280, 720), interpolation=cv2.INTER_AREA)
        return image[:, :, :3]

    def _stable_ui(self, image: np.ndarray) -> bool:
        if self._last_image is None:
            return True
        old = self._last_image
        # Life control and seven judgment nodes: exclude falling notes and
        # large animated stage backgrounds from the normal stability test.
        regions = [(936, 24, 1190, 82)] + [(int(x)-13, 582, int(x)+14, 600)
                                         for x in (190, 340, 490, 640, 790, 940, 1090)]
        differences = [float(np.abs(image[y1:y2, x1:x2].astype(np.int16)
                                   - old[y1:y2, x1:x2].astype(np.int16)).mean())
                       for x1, y1, x2, y2 in regions]
        small = cv2.resize(image, (64, 36)).astype(np.int16)
        previous = cv2.resize(old, (64, 36)).astype(np.int16)
        global_change = float(np.abs(small - previous).mean())
        return (max(differences) <= self.config.roi_change_threshold
                and global_change <= self.config.whole_frame_change_threshold)

    def _heads(self, image: np.ndarray, now: float) -> list[ObservedHead]:
        detector = self._detector
        detected = detector.detect(image, now) if hasattr(detector, "detect") else detector(image, now)
        result: list[ObservedHead] = []
        for head in detected:
            if isinstance(head, ObservedHead):
                converted = head
            else:
                kind = getattr(head.kind, "value", head.kind)
                kind = {"skill": "tap", "hold-head": "hold"}.get(str(kind), str(kind))
                y = float(head.y)
                if getattr(head, "kind", None) == NoteKind.HOLD:
                    if float(getattr(head, "hold_body_confidence", 0.0)) < .5:
                        continue
                    y += float(head.height) / 2
                converted = ObservedHead(int(head.lane), kind, y,
                                         float(getattr(head, "x", 0)),
                                         float(getattr(head, "width", 0)),
                                         bool(getattr(head, "is_head", True)))
            if (converted.is_head and converted.kind in {"tap", "hold", "flick"}
                    and 0 <= converted.lane < 7 and math.isfinite(converted.y)
                    and self.config.min_head_y <= converted.y < self.config.judgement_y):
                result.append(converted)
        return result

    def _check_life(self, image: np.ndarray, now: float) -> None:
        if now < self._next_life_check:
            return
        self._next_life_check = now + .1
        reading = self._life.detect(image)
        if not reading.visible:
            self._life_drop_streak = 0
            return
        self._life_peak = max(self._life_peak or 0, int(reading.value))
        drop = self._life_peak - int(reading.value)
        self._life_drop_streak = self._life_drop_streak + 1 if drop >= 50 else 0
        if self._life_drop_streak >= 3:
            self._reject("life-dropped-before-first-group", now)

    def observe(self, sample: FrameSample) -> FirstGroupPrediction | None:
        if self.state == "Playing" or self._prediction is not None or self.rejected_reason:
            return None
        now = float(sample.captured_at)
        capture_id = sample.capture_id
        uncertainty = float(sample.uncertainty_ms)
        capture_id_valid = isinstance(capture_id, (int, np.integer))
        monotonic_id = (capture_id_valid and (self._max_capture_id is None
                                             or int(capture_id) > self._max_capture_id))
        if bool(sample.is_new) and monotonic_id:
            self._max_capture_id = int(capture_id)
        eligible = (bool(sample.is_new) and monotonic_id
                    and math.isfinite(now) and math.isfinite(uncertainty)
                    and math.isfinite(float(sample.consumed_at)) and uncertainty >= 0
                    and uncertainty <= self.config.max_capture_uncertainty_ms
                    and 0 <= float(sample.age_ms) <= self.config.max_age_ms
                    and bool(getattr(sample, "eligible_for_start", True)))
        if not eligible:
            self.ignored_frames += 1
            return None
        self.fresh_frames += 1
        if self._last_frame_s is not None:
            gap = now - self._last_frame_s
            if gap <= 0:
                self.ignored_frames += 1
                return None
            if gap * 1000 > self.config.max_frame_gap_ms + 1e-6:
                self.gap_events += 1
                self._event("capture-gap", now, gap_ms=gap * 1000)
                if self._tracks or self._first_candidate_seen is not None:
                    self._reject("capture-gap-after-first-head-no-rebase", now)
                    return None
                self._reset()
                self._state("PlayfieldSeen", "fresh-readiness-required-after-gap", now)
        self._last_frame_s = now
        image = self._canonical(sample.image)
        if not self._playfield(image):
            if self._tracks or self._first_candidate_seen is not None:
                self._reject("playfield-lost-during-first-group", now)
                return None
            self._reset()
            self._state("WaitingPlayfield", "awaiting-playfield", now)
            return None
        if self._popup(image):
            self.member_wait_frames += 1
            if self._first_candidate_seen is not None:
                self._reject("member-popup-after-first-head-no-rebase", now)
                return None
            self._reset()
            self._state("MembersWaiting", "members-loading", now)
            return None
        self._check_life(image, now)
        if self.rejected_reason:
            return None
        stable = self._stable_ui(image)
        self._last_image = image
        if self.state in {"WaitingPlayfield", "PlayfieldSeen", "MembersWaiting", "MembersReadyCandidate"}:
            if self.state in {"WaitingPlayfield", "MembersWaiting"}:
                self._state("PlayfieldSeen", "playfield-visible-not-started", now)
            if not stable:
                self._ready_since = None
                self._ready_count = 0
                self._state("MembersReadyCandidate", "ui-fade-readiness-reset", now)
                return None
            if self._ready_since is None and self._heads(image, now):
                # If readiness starts after notes are already on screen,
                # later blank frames cannot establish a new "first" group.
                self._reject("note-present-before-members-readiness", now)
                return None
            if self._ready_since is None:
                self._ready_since = now
            self._ready_count += 1
            self._state("MembersReadyCandidate", "confirming-fresh-clear-frames", now)
            if (self._ready_count < self.config.ready_frames
                    or (now - self._ready_since) * 1000 < self.config.ready_span_ms):
                return None
            self._state("FirstGroupTracking", "members-ready-no-input", now)
        elif not stable:
            # A fade during tracking invalidates identity rather than allowing
            # a later group to establish a new anchor.
            if self._tracks or self._first_candidate_seen is not None:
                self._reject("ui-transition-during-first-group", now)
                return None
            self._reset()
            self._state("MembersReadyCandidate", "ui-fade-readiness-reset", now)
            return None
        heads = self._heads(image, now)
        self._head_history.append((now, heads))
        if not heads:
            if self._first_candidate_seen is None:
                self._clean_seen = True
            elif not self._tracks:
                self._reject("leading-unconfirmed-head-disappeared-no-rebase", now)
            elif (self._tracks and (now - max(rows[-1][0] for rows in self._tracks.values())) * 1000
                  > self.config.max_frame_gap_ms + 1e-6):
                self._reject("first-group-disappeared-no-rebase", now)
            return None
        if not self._clean_seen:
            self._reject("no-clean-prelude-before-first-head", now)
            return None
        front_y = max(head.y for head in heads)
        front = [head for head in heads if front_y - head.y <= self.config.head_group_y_tolerance]
        expected = _signature(self.prefix[0])
        actual = tuple(sorted((head.lane, head.kind) for head in front))
        if self._front_y is not None and front_y < self._front_y - 4:
            self._reject("leading-head-jumped-back-no-rebase", now)
            return None
        if (self._front_signature is not None
                and any(key not in expected for key in self._front_signature)
                and actual != self._front_signature):
            self._reject("unconfirmed-leading-head-identity-changed-no-rebase", now)
            return None
        self._front_y = front_y
        self._front_signature = actual
        if self._first_candidate_seen is None:
            self._first_candidate_seen = now
            if front_y > self.config.max_initial_head_y:
                self._reject("first-head-first-seen-too-late", now)
                return None
        if front_y > self.config.max_head_y:
            self._reject("first-group-deadline-missed", now)
            return None
        if any(key not in expected for key in actual):
            previous = self._mismatch
            if previous is not None and previous[2] == actual and front_y - previous[1] >= 12:
                self._reject("leading-group-does-not-match-chart-first-group", now)
            else:
                self._mismatch = (now, front_y, actual)
            return None
        self._mismatch = None
        if not self._tracks and front_y > self.config.max_initial_head_y:
            self._reject("first-matching-head-first-seen-too-late", now)
            return None
        for head in front:
            key = (head.lane, head.kind)
            rows = self._tracks.setdefault(key, deque(maxlen=8))
            if rows and head.y < rows[-1][1] - 4:
                self._reject("head-identity-jumped-back-no-rebase", now)
                return None
            if rows and (now - rows[-1][0]) * 1000 > self.config.max_frame_gap_ms + 1e-6:
                self._reject("first-head-observation-gap-no-rebase", now)
                return None
            rows.append((now, head.y, uncertainty))
        if actual != expected or any(key not in self._tracks for key in expected):
            self.reason = "waiting-for-every-chord-head"
            return None
        fits = [self._fit(self._tracks[key], now) for key in expected]
        if any(fit is None for fit in fits):
            self.reason = "collecting-first-group-trajectory"
            return None
        valid = [fit for fit in fits if fit is not None]
        due = float(np.median([fit[0] for fit in valid]))
        disagreement = max(fit[0] for fit in valid) - min(fit[0] for fit in valid)
        uncertainty_ms = max(fit[1] for fit in valid) + disagreement * 500
        self.candidate = {"first_due_s": due, "uncertainty_ms": uncertainty_ms,
                          "lanes": [head.lane for head in self.prefix[0]],
                          "chord_disagreement_ms": disagreement * 1000,
                          "lead_ms": (due - float(sample.consumed_at)) * 1000}
        if disagreement * 1000 > self.config.chord_agreement_ms:
            self.reason = "chord-crossings-disagree"
            return None
        # A repeated prefix needs another distinguishable visible group.
        # A trajectory alone cannot tell the first repeated group from a
        # later identical group. Never silently accept that ambiguity.
        repeats = [i for i, group in enumerate(self.prefix[1:], 1) if _signature(group) == expected]
        if repeats and not self._prefix_corroborated(heads, due, valid[0][2], repeats):
            self.reason = "repetitive-prefix-needs-corroboration"
            return None
        if uncertainty_ms > self.config.max_uncertainty_ms:
            self.reason = "prediction-uncertainty-too-large"
            return None
        lead_ms = (due - float(sample.consumed_at)) * 1000
        if lead_ms < self.config.min_send_lead_ms:
            self._reject("first-group-send-deadline-missed", now)
            return None
        if lead_ms > self.config.max_prediction_lead_ms:
            self.reason = "prediction-too-far-from-judgement"
            return None
        confidence = max(.90, 1.0 - .10 * uncertainty_ms / self.config.max_uncertainty_ms)
        self._prediction = FirstGroupPrediction(due, uncertainty_ms, confidence, now,
                                                self.prefix[0][0].time_s,
                                                tuple(head.lane for head in self.prefix[0]))
        self._event("first-group-predicted", now, **self.candidate, confidence=confidence)
        self.reason = "first-group-clock-locked-awaiting-owner"
        return self._prediction

    def _fit(self, rows: deque[tuple[float, float, float]], now: float) -> tuple[float, float, float] | None:
        if len(rows) < self.config.min_track_frames:
            return None
        if rows[-1][1] < self.config.prediction_head_y or rows[-1][1] - rows[0][1] < self.config.min_travel_px:
            return None
        ts = np.asarray([row[0] - rows[0][0] for row in rows], dtype=float)
        ys = np.asarray([row[1] for row in rows], dtype=float)
        velocity, intercept = np.polyfit(ts, ys, 1)
        if not 80 <= velocity <= 5000 or np.any(np.diff(ys) < -3):
            return None
        residual_px = float(np.max(np.abs(ys - (intercept + velocity * ts))))
        horizon = (self.config.judgement_y - ys[-1]) / velocity
        due = rows[0][0] + (self.config.judgement_y - intercept) / velocity
        # Bound extrapolation error by the measured local speed variation,
        # plus two pixels of geometry quantization and capture interval.
        slopes = np.diff(ys) / np.diff(ts)
        speed_spread = float(np.max(np.abs(slopes - velocity))) / velocity
        uncertainty_ms = (max(row[2] for row in rows)
                          + (residual_px + 2.0) / velocity * 1000
                          + max(0, horizon) * speed_spread * 1000)
        if not all(math.isfinite(value) for value in (due, uncertainty_ms)):
            return None
        return float(due), float(uncertainty_ms), float(velocity)

    def _prefix_corroborated(self, heads: list[ObservedHead], first_due: float,
                            velocity: float, repeat_indices: list[int]) -> bool:
        # Need a later distinguishable pattern that no repeated-start suffix
        # also predicts at that relative chart time. Matching geometry is a
        # conservative corroboration, not a license to rebase a missed group.
        now = self._last_frame_s
        assert now is not None
        origin = self.prefix[0][0].time_s
        for index, group in enumerate(self.prefix[1:], 1):
            relative = group[0].time_s - origin
            signature = _signature(group)
            ambiguous = False
            for shift in repeat_indices:
                shifted = [later for later in self.prefix[shift+1:]
                           if abs(later[0].time_s - self.prefix[shift][0].time_s - relative) < .025]
                # A truncated suffix is unknown evidence, not proof that a
                # repeated group is unique.
                if (self.prefix[-1][0].time_s - self.prefix[shift][0].time_s < relative - .025
                        or any(_signature(later) == signature for later in shifted)):
                    ambiguous = True
                    break
            if ambiguous:
                continue
            matching_frames: list[tuple[float, float]] = []
            for frame_s, frame_heads in self._head_history:
                predicted_y = self.config.judgement_y - velocity * (first_due + relative - frame_s)
                if not self.config.min_head_y <= predicted_y <= self.config.max_head_y:
                    continue
                matching = [head for head in frame_heads if abs(head.y - predicted_y) <= 12]
                if tuple(sorted((head.lane, head.kind) for head in matching)) == signature:
                    matching_frames.append((frame_s, float(np.mean([head.y for head in matching]))))
            if (len(matching_frames) >= self.config.min_track_frames
                    and matching_frames[-1][1] - matching_frames[0][1] >= self.config.min_travel_px
                    and now - matching_frames[-1][0] < .001):
                return True
        return False

    def report(self) -> dict[str, Any]:
        prediction = self._prediction
        return {
            "schema_version": 1, "state": self.state, "reason": self.reason,
            "rejected_reason": self.rejected_reason, "fresh_frames": self.fresh_frames,
            "ignored_frames": self.ignored_frames, "member_wait_frames": self.member_wait_frames,
            "capture_gap_events": self.gap_events, "ready_fresh_frames": self._ready_count,
            "clean_prelude_seen": self._clean_seen, "candidate": self.candidate,
            "prediction": ({"first_due_s": prediction.first_due_s,
                            "uncertainty_ms": prediction.uncertainty_ms,
                            "confidence": prediction.confidence,
                            "captured_at": prediction.captured_at,
                            "first_chart_time_s": prediction.first_chart_time_s,
                            "lanes": list(prediction.lanes)} if prediction else None),
            "prefix": [[{"time_s": head.time_s, "lane": head.lane, "kind": head.kind,
                         "direction": head.direction, "width": head.width,
                         "note_index": head.note_index} for head in group] for group in self.prefix],
            "events": list(self._events),
            "time_basis": "capture-request-completion-midpoint-estimate",
            "compensation": "prediction-before-profile-bias-no-photogate-190ms",
        }
