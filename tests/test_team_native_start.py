"""Offline start-gate regression; no emulator, device backend or touch input."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from realtime.native_play import NativeStartPhotogate
from realtime.prepare_popup import CooperativePreparePopupDetector
from realtime.life_monitor import LifeReading
from realtime.vision_io import imread_unicode
from realtime.engine import RealtimeEngine
from realtime.debug_recorder import RealtimeDebugRecorder
import tempfile
import json

class TeamPopupTests(unittest.TestCase):
    def test_real_waiting_popup_and_scale_animation_remain_blocked(self):
        image=imread_unicode(ROOT/'tests/fixtures/team/waiting-members.png')
        detector=CooperativePreparePopupDetector(verify_content=True)
        self.assertTrue(detector(image))
        popup=image[418:534,375:904]
        for scale in (.25,.4,.65,.85,1.):
            with self.subTest(scale=scale):
                resized=cv2.resize(popup,None,fx=scale,fy=scale)
                h,w=resized.shape[:2]
                sample=np.zeros_like(image)
                sample[476-h//2:476-h//2+h,640-w//2:640-w//2+w]=resized
                self.assertTrue(detector(sample))

    def test_white_pink_effect_without_text_is_not_a_team_popup(self):
        image=np.zeros((720,1280,3),np.uint8)
        cv2.rectangle(image,(410,445),(870,500),(255,255,255),-1)
        cv2.rectangle(image,(440,461),(470,490),(150,60,255),-1)
        self.assertTrue(CooperativePreparePopupDetector()(image))
        self.assertFalse(CooperativePreparePopupDetector(verify_content=True)(image))

    def test_hollow_white_note_ring_is_not_a_team_popup(self):
        image=np.zeros((720,1280,3),np.uint8)
        cv2.rectangle(image,(410,430),(870,520),(255,255,255),6)
        cv2.rectangle(image,(440,450),(470,490),(150,60,255),-1)
        self.assertTrue(CooperativePreparePopupDetector()(image))
        self.assertFalse(CooperativePreparePopupDetector(verify_content=True)(image))

    def test_recorded_results_and_track_frames_are_not_wait_popups(self):
        detector=CooperativePreparePopupDetector(verify_content=True)
        for path in (ROOT/'docs/team-live-recording-review').glob('frame-*.png'):
            if path.name=='frame-061.000s.png': continue
            self.assertFalse(detector(imread_unicode(path)),path.name)

class TeamGateTests(unittest.TestCase):
    def gate(self,**kwargs):
        return NativeStartPhotogate(mode='team-playfield-confirmed',
            playfield_detector=lambda image: True,stable_duration_ms=120,**kwargs)

    def test_popup_can_wait_longer_than_16_seconds_without_timeout(self):
        image=imread_unicode(ROOT/'tests/fixtures/team/waiting-members.png')
        gate=self.gate()
        for t in np.arange(0.,30.,.1):
            self.assertIsNone(gate.observe(image,float(t)))
        self.assertIsNone(gate.startup_rejected_reason)
        self.assertGreater(gate.prepare_popup_frames,250)

    def test_life_drop_before_start_rejects_late_chart_anchor(self):
        image=np.zeros((720,1280,3),np.uint8)
        gate=self.gate(popup_detector=lambda image: True)
        gate._startup_life=NS(detect=Mock(side_effect=[LifeReading(True,v) for v in (1000,1000,900,900,900)]))
        for t in (0.,.11,.22,.33): self.assertIsNone(gate.observe(image,t))
        with self.assertRaisesRegex(RuntimeError,'拒绝从歌曲中段启动'):
            gate.observe(image,.44)
        self.assertFalse(gate.triggered)
        with self.assertRaisesRegex(RuntimeError,'拒绝从歌曲中段启动'):
            gate.observe(image,.55)

    def test_single_life_flash_does_not_abort_waiting(self):
        image=np.zeros((720,1280,3),np.uint8)
        gate=self.gate(popup_detector=lambda image: True)
        gate._startup_life=NS(detect=Mock(side_effect=[LifeReading(True,v) for v in (1000,700,1000,1000)]))
        for t in (0.,.11,.22,.33): self.assertIsNone(gate.observe(image,t))
        self.assertIsNone(gate.startup_rejected_reason)

    def test_false_popup_no_longer_resets_first_note_baseline(self):
        # Reproduce the old false-positive shape above the timing band. The
        # first actual note changes only a narrow portion of that band.
        image=np.zeros((720,1280,3),np.uint8)
        cv2.rectangle(image,(410,445),(870,490),(255,255,255),-1)
        cv2.rectangle(image,(440,451),(470,480),(150,60,255),-1)
        gate=self.gate()
        gate._startup_life=None
        for t in np.arange(0.,.9,1/60): self.assertIsNone(gate.observe(image,float(t)))
        note=image.copy()
        note[510:536,450:550]=255
        anchor=gate.observe(note,.91)
        self.assertIsNotNone(anchor)
        self.assertLess(anchor,1.2)
        self.assertEqual(gate.prepare_popup_frames,0)

    def test_cooperative_checks_popup_content_and_single_keeps_its_policy(self):
        for mode in ('single-playfield-first-note','cooperative-playfield-confirmed'):
            gate=NativeStartPhotogate(mode=mode)
            if mode == "cooperative-playfield-confirmed":
                self.assertIsNotNone(gate._startup_life)
            else:
                self.assertIsNone(gate._startup_life)
            if gate._popup_detector:
                self.assertTrue(gate._popup_detector.verify_content)

class GateRecordingTests(unittest.TestCase):
    def run_waiting_engine(self, video_enabled, *, reject=False):
        ticks=[0.]
        frame_count=[0]
        image=imread_unicode(ROOT/'tests/fixtures/team/waiting-members.png')
        def clock():
            ticks[0]+=.005
            return ticks[0]
        def capture():
            frame_count[0]+=1
            return image
        backend=NS(exclusive=True,arm=Mock(),stop=Mock(),report=Mock(return_value={}),
            start=Mock(),observe_start_frame=Mock(return_value=None),
            start_gate_diagnostics=Mock(return_value={'photogate_popup_active':True}))
        if reject: backend.observe_start_frame.side_effect=RuntimeError('first note missed')
        planner=NS()
        touch=NS(close=Mock(),timing_offset_ms=0)
        with tempfile.TemporaryDirectory() as directory:
            recorder=RealtimeDebugRecorder(Path(directory),video_enabled=video_enabled)
            engine=RealtimeEngine(NS(),planner,touch,clock=clock,native_backend=backend,debug_recorder=recorder)
            args=dict(capture=capture,stopping=lambda: frame_count[0]>=12,
                      duration_seconds=2,target_fps=60,startup_timeout_seconds=2)
            if reject:
                with self.assertRaisesRegex(RuntimeError,'first note missed'): engine.run(**args)
            else:
                stats=engine.run(**args)
                self.assertTrue(stats.stopped)
            rows=[json.loads(line) for line in (recorder.output_dir/'trace.jsonl').read_text().splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(row['phase']=='native-first-note-gate' for row in rows))
            summary=json.loads((recorder.output_dir/'summary.json').read_text())
            self.assertGreater(summary['event_screenshots'],0)
            if video_enabled: self.assertGreater(summary['video_frames'],0)
            if reject: self.assertEqual(rows[-1]['diagnostics'][0]['event'],'native-first-note-rejected')
        backend.start.assert_not_called()
        backend.stop.assert_called_once()
        touch.close.assert_called_once()

    def test_trace_only_preserves_gate_events_and_evidence_without_starting_input(self):
        self.run_waiting_engine(False)

    def test_full_recording_includes_pretrigger_frames(self):
        self.run_waiting_engine(True)

    def test_gate_rejection_saves_evidence_and_stops_backend(self):
        self.run_waiting_engine(False,reject=True)

if __name__=='__main__': unittest.main()
