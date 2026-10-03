"""Offline regression tests: no emulator input, no live Maa agent/socket."""
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from contextlib import ExitStack
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'agent'))
from maa.agent.agent_server import AgentServer

# Decorator registration alone starts native IPC on this SDK. Unit tests replace
# both decorators, while exercising the real Python actions/recognizers below.
REGISTERED = {}
def register(name):
    def decorate(cls):
        if name in REGISTERED:
            raise AssertionError(f'duplicate registration: {name}')
        REGISTERED[name] = cls
        return cls
    return decorate

with patch.object(AgentServer, 'custom_action', register), patch.object(AgentServer, 'custom_recognition', register):
    import server
    from realtime import team_action as ta
    from realtime import formal_preflight as fp
    from realtime import difficulty_action as da

from realtime.team_recognition import TeamRecognizer, TeamScreen, DIFFICULTY_TARGETS, SONG_LEVEL_ROI, SONG_TITLE_ROI
from realtime.multiplayer_mode import is_multiplayer_mode, uses_multiplayer_start_gate
from realtime.native_play import resolve_native_start_gate_policy
from realtime.song_title_ocr import recognize_song_title, FINAL_COVER_TITLE_ROI
from realtime.final_cover import FinalCoverResolver
from realtime.chart_repository import LocalChartRepository
from realtime.vision_io import imread_unicode

FRAMES = ROOT/'docs/team-live-recording-review'
def frame(stamp):
    image = imread_unicode(FRAMES/f'frame-{stamp}s.png')
    if image is None:
        raise AssertionError(f'Missing recorded fixture {stamp}')
    return image

class Clock:
    def __init__(self): self.now = 0.0
    def time(self): return self.now
    def advance(self, seconds): self.now += seconds

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch.object(ta.time, 'monotonic', self.clock.time).start()
        patch.object(ta, 'discard_prearmed_backend').start()
        patch.object(ta, 'require_game_foreground').start()
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.flow = ta.TeamLiveFlow(self.context, ta.DEFAULT_SETTINGS)
        self.flow.wait = self.clock.advance
        self.flow.click = Mock()
        self.flow.box = Mock(return_value=None)
        self.flow.evidence = Mock()
        self.flow.save_round_report = Mock()

    def states(self, states):
        states = iter(states)
        last = TeamScreen('unknown')
        def capture():
            nonlocal last
            self.flow.check_stop()
            last = next(states, last)
            return last
        self.flow.capture = capture
        self.flow.recognizer = NS(observe=lambda image, **kw: image)

    def test_wait_never_clicks_start_below_or_at_capacity(self):
        self.states([TeamScreen(s) for s in ['lobby', 'lobby', 'full', 'matching', 'random_song']]+[TeamScreen('prepare', (1123,646))])
        self.flow.wait_for_preparation()
        self.flow.click.assert_not_called()

    def test_wait_accepts_skipped_transitions(self):
        self.states([TeamScreen('prepare', (1123,646))])
        self.flow.wait_for_preparation()
        self.flow.click.assert_not_called()

    def test_wait_timeout_never_starts_partial_room(self):
        self.states([TeamScreen('lobby')])
        with self.assertRaisesRegex(RuntimeError, '超时'):
            self.flow.wait_for_preparation()
        self.flow.click.assert_not_called()

    def test_early_loading_rejected(self):
        for state in ['ready_wait', 'loading']:
            self.states([TeamScreen(state)])
            with self.assertRaisesRegex(RuntimeError, '未验证'):
                self.flow.wait_for_preparation()

    def test_room_return_rejected(self):
        self.states([TeamScreen('home')]*2)
        with self.assertRaisesRegex(RuntimeError, '退回'):
            self.flow.wait_for_preparation()

    def test_stop_prevents_capture_and_input(self):
        self.context.tasker.stopping = True
        with self.assertRaises(InterruptedError):
            ta.TeamLiveFlow.capture(self.flow)
        with self.assertRaises(InterruptedError):
            ta.TeamLiveFlow.click(self.flow, (10,10))
        self.context.tasker.controller.post_click.assert_not_called()

    def test_connection_retry_is_spaced_and_bounded(self):
        error = TeamScreen('connect_error', (635,525))
        for _ in range(3):
            self.assertTrue(self.flow.handle_error(None,error))
            self.flow.handle_error(None,error)
            self.clock.advance(2)
        self.assertEqual(self.flow.click.call_count, 3)
        with self.assertRaisesRegex(RuntimeError, '重试3次'):
            self.flow.handle_error(None,error)

    def test_unverified_preparation_prevents_ready(self):
        ta.reset_live_run(mode='team', difficulty='Expert')
        with self.assertRaisesRegex(RuntimeError,'准备证据'):
            self.flow.ready_and_wait_for_cover()
        self.flow.click.assert_not_called()

    def test_recorded_expert_preparation_reaches_ready_and_final_cover(self):
        prep=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-off.png')
        cover=imread_unicode(ROOT/'tests/fixtures/team/northern-lights-cover.png')
        recognizer=TeamRecognizer()
        self.flow.recognizer=recognizer
        self.flow.capture=Mock(return_value=prep)
        controller=self.context.tasker.controller
        controller.post_screencap.return_value.wait.return_value.get.return_value=prep
        mapping={'TeamPrepareTitle':'prepare','TeamCutInOff':'cut_in_off'}
        self.context.run_recognition=lambda node,img: NS(hit=bool(recognizer.box(img,mapping[node])))
        with patch.object(da,'require_game_foreground'), patch.object(da.time,'sleep'), patch.object(da,'capture_image',return_value=prep), patch.object(fp,'require_game_foreground'), patch.object(fp,'capture_image',return_value=prep), patch.object(ta.RealtimePerformanceSettingsGate,'run',return_value=True):
            self.flow.prepare()
        # Only the Expert button was clicked; gray Cut in remains untouched.
        controller.post_click.assert_called_once_with(852,575)
        self.assertEqual(ta.current_live_run().song_level,26)
        self.flow.capture=Mock(side_effect=[prep,frame('047.062'),np.zeros_like(prep),cover,cover])
        self.flow.ready_and_wait_for_cover()
        self.flow.click.assert_called_once_with((1123,646))
        self.assertEqual(ta.current_live_run().startup_final_cover_resolution.selection.bestdori_song_id,309)

    def test_verified_cover_handoff_keeps_same_round(self):
        self.flow.require_safe_preparation=Mock()
        run=ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        prepare,unknown=TeamScreen('prepare',(1123,646)),TeamScreen('unknown')
        self.flow.capture=Mock(return_value=np.zeros((720,1280,3),np.uint8))
        self.flow.recognizer=NS(observe=Mock(side_effect=[prepare,unknown,unknown]))
        resolution=object()
        resolver=Mock(observe=Mock(side_effect=[None,resolution]))
        with patch.object(ta,'FinalCoverResolver',return_value=resolver), patch.object(ta,'identify_final_song',return_value=NS(song_id=ta.UNKNOWN_SONG_ID)), patch.object(ta,'PlayfieldDetector',return_value=Mock(return_value=False)):
            self.flow.ready_and_wait_for_cover()
        after=ta.current_live_run()
        self.assertEqual(after.run_id,run.run_id)
        self.assertIs(after.startup_final_cover_resolution,resolution)
        self.flow.click.assert_called_once_with((1123,646))

    def test_result_sequence_uses_confirm_then_home_without_again(self):
        images=iter([frame(s) for s in ['186.000','204.000','212.062','222.078','240.000','241.593']])
        self.flow.recognizer=TeamRecognizer()
        self.flow.capture=lambda: next(images)
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if node=='TeamHomeMarker' and self.flow.recognizer.observe(image,results=True).state=='unknown' else None
        self.flow.wait=lambda seconds: self.clock.advance(1)
        ta.reset_live_run(mode='team',difficulty='Expert')
        with patch.object(ta,'imwrite_unicode'):
            self.assertTrue(self.flow.return_home())
        points=[c.args[0] for c in self.flow.click.call_args_list]
        self.assertEqual(len(points),4)
        self.assertTrue(all(x>1000 for x,y in points),points)

    def test_cancel_is_never_clicked_after_ready_ack(self):
        self.flow.require_safe_preparation=Mock()
        ta.reset_live_run(mode='team', difficulty='Expert', prepared_for_play=True)
        self.states([TeamScreen('prepare',(1123,646)),TeamScreen('ready_wait'),TeamScreen('loading')])
        with self.assertRaisesRegex(RuntimeError, '180 秒无可确认进展'):
            self.flow.ready_and_wait_for_cover()
        self.flow.click.assert_called_once_with((1123,646))

    def test_black_does_not_start_play_and_track_without_cover_fails(self):
        self.flow.require_safe_preparation=Mock()
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        self.states([TeamScreen('prepare',(1123,646)),TeamScreen('unknown')])
        resolver=Mock(last_reason='missing',observe=Mock(return_value=None))
        with patch.object(ta,'FinalCoverResolver',return_value=resolver), patch.object(ta,'identify_final_song',return_value=NS(song_id=ta.UNKNOWN_SONG_ID)), patch.object(ta,'PlayfieldDetector',return_value=Mock(side_effect=[False,False,True,True])):
            with self.assertRaisesRegex(RuntimeError, '禁止从歌曲中段'):
                self.flow.ready_and_wait_for_cover()
        self.assertEqual(resolver.observe.call_count, 4)

    def test_boolean_play_success_is_not_completion(self):
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        with patch.object(ta.RealtimeProfilePlay, 'run', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '未确认本局完成'):
                self.flow.play()

    def test_3d_mode_at_ready_is_rejected_before_any_ready_input(self):
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        self.flow.capture=Mock(return_value=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png'))
        with self.assertRaisesRegex(RuntimeError,'演出模式／Cut in'):
            self.flow.ready_and_wait_for_cover()
        self.flow.click.assert_not_called()

    def test_prepare_loading_or_play_failure_uses_escape_before_retry(self):
        for phase in ('prepare','loading','play'):
            with self.subTest(phase=phase):
                self.flow.completed_ids.clear()
                def fail():
                    self.flow.phase=phase
                    raise RuntimeError('mode changed or cover missed')
                attempts=[fail,lambda: NS(run_id=phase)]
                self.flow.run_attempt=Mock(side_effect=lambda: attempts.pop(0)())
                self.flow.recover_home=Mock()
                self.flow.restart_after_failure=Mock()
                self.flow.return_home=Mock()
                self.assertTrue(self.flow.run())
                self.assertEqual(self.flow.run_attempt.call_count,2)
                self.flow.restart_after_failure.assert_called_once_with('mode changed or cover missed',disconnect=True)
                self.flow.recover_home.assert_not_called()
                self.assertEqual(self.flow.completed_ids,{phase})

    def test_fast_capture_is_used_for_matching_preparation_and_loading(self):
        image=frame('035.000')
        with patch.object(ta,'capture_image',return_value=image) as capture:
            for phase in ('matching','prepare','loading'):
                self.flow.phase=phase
                ta.TeamLiveFlow.capture(self.flow)
                capture.assert_called_with(self.context,node='TeamRefreshScreen')
            self.flow.phase='result'
            ta.TeamLiveFlow.capture(self.flow)
            capture.assert_called_with(self.context,node='ResultRefreshScreen')

    def test_play_requires_same_completed_run(self):
        original=ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        def complete(*args):
            ta.update_live_run(play_completed=True)
            return True
        with patch.object(ta.RealtimeProfilePlay,'run',side_effect=complete):
            self.assertEqual(self.flow.play().run_id,original.run_id)
        def replace(*args):
            ta.reset_live_run(mode='team',difficulty='Expert')
            ta.update_live_run(play_completed=True)
            return True
        with patch.object(ta.RealtimeProfilePlay,'run',side_effect=replace):
            with self.assertRaises(RuntimeError): self.flow.play()

    def test_result_failure_recovers_without_replaying_completed_song(self):
        run=NS(run_id='one')
        self.flow.run_attempt=Mock(return_value=run)
        self.flow.return_home=Mock(side_effect=RuntimeError('result timeout'))
        self.flow.recover_home=Mock()
        self.assertTrue(self.flow.run())
        self.flow.run_attempt.assert_called_once()
        self.flow.recover_home.assert_called_once()
        self.assertEqual(self.flow.save_round_report.call_args_list[0],
            unittest.mock.call(run,False,'结算进行中',returned_home=False))
        self.assertEqual(self.flow.save_round_report.call_args_list[-1],
            unittest.mock.call(run,False,'result timeout'))

    def test_multiple_rounds_settle_before_reentry_and_stop_at_limit(self):
        self.flow.settings['count']=3
        events=[]
        def attempt():
            number=len(self.flow.completed_ids)+1
            if number>1: self.assertEqual(events[-1],f'home-{number-1}')
            events.append(f'play-{number}')
            return NS(run_id=str(number))
        def settle(): events.append(f'home-{len(self.flow.completed_ids)}')
        self.flow.run_attempt=Mock(side_effect=attempt)
        self.flow.return_home=Mock(side_effect=settle)
        self.flow.progress=Mock()
        self.assertTrue(self.flow.run())
        self.assertEqual(events,['play-1','home-1','play-2','home-2','play-3','home-3'])
        self.assertEqual(self.flow.progress.call_args_list,
            [unittest.mock.call(i,3) for i in [1,2,3]])
        self.assertEqual(self.flow.run_attempt.call_count,3)

    def test_next_round_retries_reset_and_do_not_count_failures(self):
        self.flow.settings.update(count=2,max_retries=1)
        self.flow.run_attempt=Mock(side_effect=[RuntimeError('first entry'),NS(run_id='1'),
            RuntimeError('second entry'),NS(run_id='2')])
        self.flow.return_home=Mock()
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.completed_ids,{'1','2'})
        self.assertEqual(self.flow.return_home.call_count,2)

    def test_stop_during_settlement_preserves_completed_round(self):
        run=NS(run_id='one')
        self.flow.run_attempt=Mock(return_value=run)
        self.flow.return_home=Mock(side_effect=InterruptedError('stop'))
        with self.assertRaises(InterruptedError): self.flow.run()
        self.assertEqual(self.flow.completed_ids,{'one'})
        self.flow.save_round_report.assert_called_with(run,False,'用户在结算期间停止',returned_home=False)
        self.flow.run_attempt.assert_called_once()

    def test_failed_home_recovery_does_not_start_another_song(self):
        self.flow.settings['count']=2
        self.flow.run_attempt=Mock(return_value=NS(run_id='one'))
        self.flow.return_home=Mock(side_effect=RuntimeError('result timeout'))
        self.flow.recover_home=Mock(side_effect=RuntimeError('home unavailable'))
        with self.assertRaisesRegex(RuntimeError,'home unavailable'): self.flow.run()
        self.flow.run_attempt.assert_called_once()
        self.assertFalse(self.flow.save_round_report.call_args.kwargs['returned_home'])

    def test_new_attempt_clears_old_song_cover_and_completion(self):
        original=ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        ta.update_live_run(song_title='old song',play_completed=True,
            startup_final_cover_resolution=object(),startup_final_cover_image=np.zeros((1,1)))
        self.flow.network_retries=3
        self.flow.recover_home=Mock()
        self.flow.invoke=Mock()
        self.flow.navigate_team_home=Mock()
        self.flow.join_room=Mock()
        self.flow.wait_for_preparation=Mock()
        def prepare():
            new=ta.current_live_run()
            self.assertNotEqual(new.run_id,original.run_id)
            self.assertIsNone(new.song_title)
            self.assertIsNone(new.startup_final_cover_resolution)
            self.assertIsNone(new.startup_final_cover_image)
            self.assertFalse(new.play_completed)
            self.assertEqual(self.flow.network_retries,0)
        self.flow.prepare=prepare
        self.flow.ready_and_wait_for_cover=Mock()
        self.flow.play=Mock(return_value=NS(run_id='new'))
        with patch.object(ta,'cooperative_profile_preflight',return_value=None):
            self.flow.run_attempt()

    def test_repeated_activity_rewards_then_confirm_then_home(self):
        reward=imread_unicode(ROOT/'tests/fixtures/team/activity-reward.png')
        # A different reward icon/amount must not affect dialog recognition.
        other=reward.copy()
        other[215:450,435:860]=255
        activity=frame('222.078')
        home=frame('240.000')
        self.flow.recognizer=TeamRecognizer()
        self.flow.capture=Mock(side_effect=[reward,other,activity,home,home])
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if image is home and node=='TeamHomeMarker' else None
        self.flow.wait=lambda seconds: self.clock.advance(1)
        self.assertTrue(self.flow.return_home())
        points=[c.args[0] for c in self.flow.click.call_args_list]
        self.assertEqual(points,[(640,542),(640,542),(1068,648)])

    def test_recorded_rank_up_uses_shared_back_then_stops_all_input_at_home(self):
        rank_up=imread_unicode(ROOT/'tests/fixtures/team/character-rank-up.png')
        home=imread_unicode(ROOT/'tests/fixtures/team/home-circle.png')
        self.assertEqual(self.flow.recognizer.observe(rank_up,results=True).state,'unknown')
        current=[rank_up]
        self.flow.capture=lambda: current[0]
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if image is home and node=='TeamHomeMarker' else None
        # The replay screen changes in response to input, not merely capture.
        controller=self.context.tasker.controller
        def leave_popup(key):
            self.assertEqual(key,4)
            current[0]=home
            return Mock()
        controller.post_click_key.side_effect=leave_popup
        self.assertTrue(self.flow.return_home())
        controller.post_click.assert_called_once_with(1279,719)
        controller.post_click_key.assert_called_once_with(4)
        self.flow.click.assert_not_called()
        self.assertLess(self.clock.now,3)

    def test_unclassified_reward_chain_is_not_limited_by_popup_count(self):
        current=[TeamScreen('unknown')]
        self.flow.recognizer=NS(observe=lambda image,**kw: image)
        self.flow.capture=lambda: current[0]
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if image.state=='home' and node=='TeamHomeMarker' else None
        actions=[]
        def corner(x,y):
            actions.append(('click',x,y))
            return Mock()
        def back(key):
            actions.append(('back',key))
            if actions.count(('back',4))==8:
                current[0]=TeamScreen('home')
            return Mock()
        self.context.tasker.controller.post_click.side_effect=corner
        self.context.tasker.controller.post_click_key.side_effect=back
        self.assertTrue(self.flow.return_home())
        self.assertEqual(actions,[('click',1279,719),('back',4)]*8)
        self.flow.click.assert_not_called()

    def test_result_with_missing_button_still_advances(self):
        self.states([TeamScreen('experience')])
        controller=self.context.tasker.controller
        def leave(_key):
            self.states([TeamScreen('home')])
            return Mock()
        controller.post_click_key.side_effect=leave
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if image.state=='home' and node=='TeamHomeMarker' else None
        self.assertTrue(self.flow.return_home())
        controller.post_click.assert_called_once_with(1279,719)
        controller.post_click_key.assert_called_once_with(4)

    def test_story_after_back_is_handled_before_any_further_result_input(self):
        current=[TeamScreen('unknown')]
        self.flow.capture=lambda: current[0]
        self.flow.recognizer=NS(observe=lambda image,**kw: image)
        def box(image,node):
            if image.state=='home' and node=='TeamHomeMarker':
                return NS(x=1,y=1,w=1,h=1)
            if image.state=='story' and node=='AutoLiveStorySkipConfirmLarge':
                return NS(x=600,y=400,w=200,h=60)
        self.flow.box=box
        def back(_key):
            current[0]=TeamScreen('story')
            return Mock()
        def skip(point):
            self.assertEqual(point,(700,430))
            current[0]=TeamScreen('home')
        controller=self.context.tasker.controller
        controller.post_click_key.side_effect=back
        self.flow.click.side_effect=skip
        self.assertTrue(self.flow.return_home())
        controller.post_click.assert_called_once_with(1279,719)
        controller.post_click_key.assert_called_once_with(4)
        self.flow.click.assert_called_once_with((700,430))

    def test_result_quit_cancel_then_home_never_sends_back(self):
        self.states([TeamScreen('quit'),TeamScreen('home'),TeamScreen('home')])
        def box(image,node):
            if (image.state,node) in {('quit','QuitConfirmCancel'),('home','TeamHomeMarker')}:
                return NS(x=500,y=400,w=200,h=60)
        self.flow.box=box
        self.assertTrue(self.flow.return_home())
        self.flow.click.assert_called_once_with((600,430))
        self.context.tasker.controller.post_click.assert_not_called()
        self.context.tasker.controller.post_click_key.assert_not_called()

    def test_missing_result_destination_remains_time_bounded(self):
        self.states([TeamScreen('unknown')])
        self.flow.log=Mock()
        with patch('realtime.result_navigation.print'):
            with self.assertRaisesRegex(RuntimeError,'180秒'):
                self.flow.return_home()
        self.assertGreater(self.context.tasker.controller.post_click_key.call_count,3)
        self.assertLess(self.clock.now,181)

    def test_stop_during_result_foreground_check_prevents_shared_input(self):
        self.states([TeamScreen('unknown')])
        ta.require_game_foreground.side_effect=lambda _controller: setattr(self.context.tasker,'stopping',True)
        with self.assertRaises(InterruptedError): self.flow.return_home()
        self.context.tasker.controller.post_click.assert_not_called()
        self.context.tasker.controller.post_click_key.assert_not_called()

    def test_result_input_uses_current_controller_after_foreground_check(self):
        self.states([TeamScreen('unknown')])
        original=self.context.tasker.controller
        replacement=Mock()
        ta.require_game_foreground.side_effect=lambda _controller: setattr(self.context.tasker,'controller',replacement)
        def clicked(*_point):
            self.states([TeamScreen('home')])
            return Mock()
        replacement.post_click.side_effect=clicked
        self.flow.box=lambda image,node: NS(x=1,y=1,w=1,h=1) if image.state=='home' and node=='TeamHomeMarker' else None
        self.assertTrue(self.flow.return_home())
        original.post_click.assert_not_called()
        replacement.post_click.assert_called_once_with(1279,719)
        replacement.post_click_key.assert_not_called()

    def test_round_report_is_written_before_settlement_then_updated(self):
        run=ta.reset_live_run(mode='team',difficulty='Expert')
        self.flow.completed_ids.add(run.run_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(ta,'ROOT',Path(directory)):
            ta.TeamLiveFlow.save_round_report(self.flow,run,False,'结算进行中',returned_home=False)
            path=Path(directory)/f'screencap/team-round-{run.run_id}.json'
            pending=json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(pending['play_completed'])
            self.assertFalse(pending['returned_home'])
            self.assertEqual(pending['completed_count'],1)
            ta.TeamLiveFlow.save_round_report(self.flow,run,True)
            final=json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(final['settlement_finished'])
            self.assertTrue(final['returned_home'])

    def test_duplicate_run_is_not_counted_twice(self):
        self.flow.settings['count']=2
        self.flow.run_attempt=Mock(return_value=NS(run_id='one'))
        self.flow.return_home=Mock()
        with self.assertRaisesRegex(RuntimeError,'重复'):
            self.flow.run()
        self.flow.return_home.assert_called_once()

    def test_attempt_retries_bounded(self):
        self.flow.run_attempt=Mock(side_effect=RuntimeError('entry timeout'))
        with self.assertRaisesRegex(RuntimeError,'重试已达上限'):
            self.flow.run()
        self.assertEqual(self.flow.run_attempt.call_count,3)

    def test_entry_and_play_failure_have_separate_retry_budgets(self):
        self.flow.settings['max_retries']=1
        phases=iter(['entry','play',None])
        def attempt():
            phase=next(phases)
            if phase:
                self.flow.phase=phase
                raise RuntimeError(phase)
            return NS(run_id='one')
        self.flow.run_attempt=Mock(side_effect=attempt)
        self.flow.return_home=Mock()
        self.flow.restart_after_failure=Mock()
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_count,3)
        self.flow.return_home.assert_called_once()
        self.flow.restart_after_failure.assert_called_once_with('play',disconnect=True)

    def test_infinite_mode_stops_after_completed_round(self):
        self.flow.settings['count']=0
        self.flow.run_attempt=Mock(return_value=NS(run_id='one'))
        self.flow.return_home=Mock(side_effect=lambda: setattr(self.context.tasker,'stopping',True))
        with self.assertRaises(InterruptedError): self.flow.run()
        self.flow.run_attempt.assert_called_once()

class RecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.recognizer=TeamRecognizer()

    def test_recorded_state_sequence_and_buttons(self):
        cases=[('008.000','entry',False),('012.000','home',True),('016.000','lobby',False),
            ('022.000','lobby',False),('023.000','full',False),('026.000','matching',False),
            ('030.062','random_song',False),('035.000','prepare',True),('043.000','ready_wait',False),
            ('047.062','loading',False),('051.062','connect_error',True),
            ('186.000','team_result',True),('194.062','reward_popup',True),('198.000','achievement',True),
            ('204.000','pggbm',True),('212.062','experience',True),('222.078','activity',True)]
        for stamp,state,button in cases:
            with self.subTest(stamp=stamp):
                screen=self.recognizer.observe(frame(stamp),results=float(stamp)>170)
                self.assertEqual(screen.state,state)
                self.assertEqual(screen.button is not None,button)

    def test_activity_reward_takes_priority_over_dimmed_results(self):
        image=imread_unicode(ROOT/'tests/fixtures/team/activity-reward.png')
        self.assertEqual(self.recognizer.observe(image,results=True),TeamScreen('activity_reward',(640,542)))
        self.assertIsNone(self.recognizer.point(image,'confirm_result'))
        for path in FRAMES.glob('frame-*.png'):
            self.assertIsNone(self.recognizer.box(imread_unicode(path),'activity_reward'),path.name)

    @unittest.skipUnless(FRAMES.is_dir(), '需要本地团队录像帧，公开仓库不包含原始录像')
    def test_all_117_frames_have_no_input_during_track_or_home(self):
        files=sorted(FRAMES.glob('frame-*.png'))
        self.assertEqual(len(files),117)
        for path in files:
            seconds=float(path.stem[6:-1])
            if seconds<7 or 53<=seconds<=174 or seconds>=224:
                image=imread_unicode(path)
                self.assertIsNone(self.recognizer.observe(image).button,path.name)
                self.assertIsNone(self.recognizer.observe(image,results=True).button,path.name)

    def test_dimmed_modal_does_not_match_underlying_button(self):
        self.assertIsNone(self.recognizer.point(frame('194.062'),'next_detail'))
        self.assertIsNone(self.recognizer.point(frame('051.062'),'ready'))
        self.assertEqual(self.recognizer.observe(np.zeros((720,1280,3),np.uint8)).state,'unknown')

    def test_recorded_difficulty_and_level(self):
        for stamp,difficulty,level in [('035.000','Expert',26),('041.000','Easy',9)]:
            image=frame(stamp)
            self.assertEqual(da.selected_difficulty(image,DIFFICULTY_TARGETS),difficulty)
            self.assertEqual(da.read_song_level(image,SONG_LEVEL_ROI),level)

    def test_title_and_cover_resolve_repository_identity(self):
        title=recognize_song_title(frame('035.000'),roi=SONG_TITLE_ROI)
        self.assertIsNotNone(title)
        self.assertGreater(title.confidence,.7)
        resolver=FinalCoverResolver(difficulty='Expert',observed_level=26,
            observed_title=title.text,observed_title_confidence=title.confidence,
            repository=LocalChartRepository(ROOT/'resource/charts'))
        image=frame('055.000')
        final_title=recognize_song_title(image,roi=FINAL_COVER_TITLE_ROI)
        resolver.refresh_observed_title(final_title.text,final_title.confidence)
        self.assertIsNone(resolver.observe(image))
        result=resolver.observe(frame('057.062'))
        self.assertIsNotNone(result,resolver.last_reason)
        self.assertEqual(result.selection.bestdori_song_id,75)
        # This tests song identity; footage selected Easy while the local bundle
        # contains this song's Hard/Expert charts only. Missing Easy must fail.
        missing=LocalChartRepository(ROOT/'resource/charts').resolve(
            result.confirmation.song_id,'Easy',level=9,title=final_title.text)
        self.assertIsNone(missing.selection)

class SharedActionTests(unittest.TestCase):
    def setUp(self):
        self.controller=Mock()
        self.controller.post_screencap.return_value.wait.return_value.get.return_value=frame('035.000')
        self.context=NS(tasker=NS(stopping=False,controller=self.controller),run_recognition=Mock())
        self.addCleanup(patch.stopall)
        patch.object(fp,'require_game_foreground').start()
        patch.object(fp,'_wait',return_value=True).start()
        patch.object(fp,'capture_image',return_value=frame('035.000')).start()

    def test_existing_preflight_defaults_keep_all_checks(self):
        self.context.run_recognition.return_value=NS(hit=False)
        with patch.object(fp,'formal_live_mode_is_off',return_value=True) as mode, patch.object(fp,'cut_in_is_checked',return_value=False):
            self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args({})))
        mode.assert_called_once()
        self.context.run_recognition.assert_called_with('AutoLiveEnabled',unittest.mock.ANY)
        self.controller.post_click.assert_not_called()

    def test_gray_cutin_is_already_off_and_never_clicked(self):
        self.context.run_recognition.return_value=NS(hit=True)
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,check_performance_mode=False)
        image=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-off.png')
        self.assertFalse(fp.cut_in_is_checked(image))
        with patch.object(fp,'capture_image',return_value=image):
            self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_not_called()

    def test_pink_cutin_is_disabled_once_then_gray_passes(self):
        self.context.run_recognition.return_value=NS(hit=True)
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,check_performance_mode=False)
        pink=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-on.png')
        gray=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-off.png')
        self.assertTrue(fp.cut_in_is_checked(pink))
        with patch.object(fp,'capture_image',side_effect=[pink,gray]):
            self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_called_once_with(500,650)

    def test_real_third_round_3d_mode_cycles_before_cut_in_check(self):
        mode=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png')
        off=imread_unicode(ROOT/'tests/fixtures/team/prepare-flame-off.png')
        rec=TeamRecognizer()
        self.assertFalse(fp.formal_live_mode_is_off(mode))
        self.assertTrue(fp.formal_live_mode_is_off(off))
        self.assertIsNone(rec.box(mode,'cut_in_off'))
        self.assertIsNotNone(rec.box(off,'cut_in_off'))
        current=[mode]
        self.context.run_recognition=lambda node,img: NS(hit=bool(rec.box(img,{'guard':'prepare','off':'cut_in_off'}[node])))
        def click(x,y):
            # The avatar region must never be interpreted as the toggle.
            self.assertEqual((x,y),(141,649))
            current[0]=off
            return Mock()
        self.controller.post_click.side_effect=click
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,
                    check_performance_mode=True,refresh_node='TeamRefreshScreen',mode_settle_seconds=.25)
        with patch.object(fp,'capture_image',side_effect=lambda *a,**kw: current[0]) as capture:
            self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_called_once_with(141,649)
        self.assertEqual(capture.call_count,2)
        capture.assert_called_with(self.context,node='TeamRefreshScreen')

    def test_3d_then_enabled_cut_in_then_disabled_uses_distinct_controls(self):
        mode=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png')
        pink=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-on.png')
        gray=imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-off.png')
        rec=TeamRecognizer()
        self.context.run_recognition=lambda node,img: NS(hit=bool(rec.box(img,{'guard':'prepare','off':'cut_in_off'}[node])))
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,check_performance_mode=True)
        with patch.object(fp,'capture_image',side_effect=[mode,pink,gray]):
            self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.assertEqual(self.controller.post_click.call_args_list,
                         [unittest.mock.call(141,649),unittest.mock.call(500,650)])

    def test_mode_switch_page_expiry_never_clicks_cut_in_or_ready(self):
        mode=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png')
        loading=frame('047.062')
        rec=TeamRecognizer()
        self.context.run_recognition=lambda node,img: NS(hit=bool(rec.box(img,'prepare')))
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,check_performance_mode=True)
        with patch.object(fp,'capture_image',side_effect=[mode,loading]):
            self.assertFalse(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_called_once_with(141,649)

    def test_unknown_cutin_state_is_not_assumed_off(self):
        self.context.run_recognition.side_effect=lambda node,img: NS(hit=node=='guard')
        params=dict(page_guard='guard',cut_in_off_node='off',check_auto_live=False,check_performance_mode=False)
        self.assertFalse(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_not_called()

    def test_preflight_page_expiry_blocks_input(self):
        self.context.run_recognition.return_value=NS(hit=False)
        self.assertFalse(fp.RealtimeFormalPreflight().run(self.context,ta.action_args({'page_guard':'guard'})))
        self.controller.post_click.assert_not_called()

    def test_stop_during_gray_recognition_prevents_click(self):
        def recognize(node,image):
            if node=='gray': self.context.tasker.stopping=True
            return NS(hit=True)
        self.context.run_recognition.side_effect=recognize
        params=dict(page_guard='guard',cut_in_off_node='gray',check_auto_live=False,check_performance_mode=False)
        self.assertTrue(fp.RealtimeFormalPreflight().run(self.context,ta.action_args(params)))
        self.controller.post_click.assert_not_called()

    def test_difficulty_page_expiry_blocks_input(self):
        self.context.run_recognition.return_value=NS(hit=False)
        with patch.object(da,'require_game_foreground'):
            self.assertFalse(da.RealtimeDifficultySelect().run(self.context,ta.action_args({'difficulty':'Expert','page_guard':'guard'})))
        self.controller.post_click.assert_not_called()

    def test_multiplayer_policy_and_single_policy(self):
        for mode in ('team','cooperative'):
            self.assertTrue(is_multiplayer_mode(mode))
            policy=resolve_native_start_gate_policy(mode)
            self.assertEqual(policy.stable_duration_ms,120)
            self.assertTrue(uses_multiplayer_start_gate(policy.mode))
        for mode in ('realtime','medley','challenge',None):
            self.assertFalse(is_multiplayer_mode(mode))
            self.assertEqual(resolve_native_start_gate_policy(mode).stable_duration_ms,250)
        self.assertTrue(uses_multiplayer_start_gate('cooperative-legacy'))

    def test_team_speed_gate_uses_fast_refresh_and_defers_native(self):
        from realtime import performance_settings_action as performance
        image=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png')
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        params=dict(difficulty='Expert',refresh_node='TeamRefreshScreen',defer_native_prearm=True)
        with ExitStack() as stack:
            capture=stack.enter_context(patch.object(performance,'capture_image',return_value=image))
            stack.enter_context(patch.object(performance,'_expected_speed',return_value=(5.,'test.json')))
            stack.enter_context(patch.object(performance.RealtimeProfileStore,'runtime_options',return_value={'note_speed_settings_enabled':False}))
            stack.enter_context(patch.object(performance,'publish_verified_performance_settings'))
            stack.enter_context(patch.object(performance,'discard_prearmed_backend'))
            native=stack.enter_context(patch.object(performance,'prepare_native_for_settings_gate'))
            self.assertTrue(performance.RealtimePerformanceSettingsGate()._run(self.context,params))
        capture.assert_called_once_with(self.context,node='TeamRefreshScreen')
        self.controller.post_screencap.assert_not_called()
        self.controller.post_click.assert_not_called()
        native.assert_not_called()

class IntegrationTests(unittest.TestCase):
    def test_team_handoff_selects_native_prearm_with_confirmed_chart(self):
        from realtime import profile_play_action as play
        # Execute the real shared entry up to native device preparation. The
        # sentinel stops before constructing a device backend or sending input.
        class NativeReached(BaseException): pass
        image=imread_unicode(ROOT/'tests/fixtures/team/northern-lights-cover.png')
        resolver=FinalCoverResolver(difficulty='Expert',observed_level=26,
            observed_title='Northern lights',observed_title_confidence=.99,
            repository=LocalChartRepository(ROOT/'resource/charts'))
        resolver.observe(image)
        resolution=resolver.observe(image)
        run=ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        ta.update_live_run(song_level=26,song_title='cYanGllowb',song_title_confidence=.591,
            startup_final_cover_image=image,startup_final_cover_resolution=resolution)
        context=NS(tasker=NS(stopping=False,controller=Mock()))
        settings=NS(target_fps=60,timing_offset_ms=0,note_speed=5.,profile_path=ROOT/'profiles/test.json')
        verified=NS(expected_note_speed=5.,actual_note_speed=5.)
        params=ta.team_play_params({**ta.DEFAULT_SETTINGS,'debug_recording':False,'diagnostic_trace':False})
        with ExitStack() as stack:
            stack.enter_context(patch.object(play,'resolve_profile',return_value=settings))
            stack.enter_context(patch.object(play,'verified_settings',return_value=verified))
            store=stack.enter_context(patch.object(play,'RealtimeProfileStore'))
            store.return_value.runtime_options.return_value={'native_realtime_enabled':True,'chart_prediction_enabled':True}
            stack.enter_context(patch.object(play,'debug_enabled',return_value=False))
            prearm=stack.enter_context(patch.object(play,'prepare_native_for_settings_gate',side_effect=NativeReached))
            with self.assertRaises(NativeReached):
                play.RealtimeProfilePlay().run(context,ta.action_args(params))
        prearm.assert_called_once()
        confirmed=prearm.call_args.kwargs['live_run']
        self.assertEqual(confirmed.run_id,run.run_id)
        self.assertEqual(confirmed.mode,'team')
        self.assertTrue(confirmed.final_cover_confirmed)
        self.assertEqual(confirmed.song_title,'Northern lights')
        self.assertTrue(prearm.call_args.kwargs['runtime_options']['native_realtime_enabled'])
        context.tasker.controller.post_click.assert_not_called()

    def test_navigation_recording_starts_before_recovery_and_closes_on_stop(self):
        import screen_refresh as sr
        context=NS(tasker=NS(stopping=False,controller=NS(cached_image=frame('000.000'))),
            run_task=Mock(return_value=NS(status=NS(succeeded=True))))
        ta.configure_team_settings({'reset':True,'debug_recording':True})
        self.addCleanup(lambda: ta.configure_team_settings({'reset':True}))
        def startup(flow):
            sr.capture_image(context)
            raise InterruptedError('stop in startup recovery')
        with patch.object(ta,'RealtimeDebugRecorder') as recording, patch.object(ta.TaskProgress,'run',return_value=True), patch.object(ta.TeamLiveFlow,'run',startup):
            self.assertTrue(ta.TeamLiveAction().run(context,ta.action_args({})))
            self.assertTrue(recording.call_args.kwargs['video_enabled'])
            recorder=recording.return_value
            recorder.record_phase.assert_called_once()
            self.assertEqual(recorder.record_phase.call_args.args[2],'entry')
            recorder.close.assert_called_once()
        self.assertIsNone(sr._frame_observer.get())

    def test_capture_observer_exception_does_not_break_other_tasks(self):
        import screen_refresh as sr
        image=frame('000.000')
        context=NS(tasker=NS(stopping=False,controller=NS(cached_image=image)),
            run_task=Mock(return_value=NS(status=NS(succeeded=True))))
        with sr.observe_captured_images(Mock(side_effect=RuntimeError('diagnostic failure'))):
            self.assertIs(sr.capture_image(context),image)
        self.assertIs(sr.capture_image(context),image)

    def test_settings_reset_validation_and_isolation(self):
        from realtime.cooperative_action import current_cooperative_settings
        coop=current_cooperative_settings()
        ta.configure_team_settings({'reset':True,'difficulty':'Hard','count':'2'})
        settings=ta.current_team_settings()
        self.assertEqual(settings['count'],2)
        settings['count']=99
        self.assertEqual(ta.current_team_settings()['count'],2)
        for params in [{'count':-1},{'count':True},{'difficulty':'Special'},{'max_retries':6},{'wait_timeout_seconds':29}]:
            with self.assertRaises(ValueError): ta.configure_team_settings(params)
        self.assertEqual(ta.current_team_settings()['difficulty'],'Hard')
        self.assertEqual(ta.configure_team_settings({'reset':True}),ta.DEFAULT_SETTINGS)
        self.assertEqual(current_cooperative_settings(),coop)

    def test_team_play_owns_result_navigation_and_requests_escape_on_zero_life(self):
        params=ta.team_play_params(ta.DEFAULT_SETTINGS)
        self.assertEqual(params['run_mode'],'team')
        self.assertTrue(params['defer_result_collection'])
        self.assertTrue(params['continue_after_life_depleted'])
        self.assertTrue(params['life_depleted_jump_request'])
        self.assertTrue(params['propagate_failure'])
        self.assertTrue(params['defer_failed_exit'])

    def test_pipeline_links_templates_and_registered_actions(self):
        all_nodes={}
        for path in (ROOT/'resource/pipeline').glob('*.json'):
            nodes=json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(all_nodes.keys() & nodes.keys(),path.name)
            all_nodes.update(nodes)
        team=json.loads((ROOT/'resource/pipeline/team_live.json').read_text(encoding='utf-8'))
        for name,node in team.items():
            for field in ('next','on_error'):
                for target in node.get(field,[]): self.assertIn(target,all_nodes)
            if node.get('custom_action'): self.assertIn(node['custom_action'],REGISTERED)
            if node.get('template'): self.assertTrue((ROOT/'resource/image'/node['template']).is_file())
        self.assertEqual(team['TeamResetConfigure']['custom_action_param'],{'reset':True})

    def test_both_interfaces_have_valid_overrides_and_keep_agent_paths(self):
        team=json.loads((ROOT/'resource/pipeline/team_live.json').read_text(encoding='utf-8'))
        interfaces=[]
        for filename in ['interface.template.json','interface.json']:
            if not (ROOT/filename).exists() and filename == 'interface.template.json':
                continue  # 源码只有 interface.json，模板由发布构建生成。
            data=json.loads((ROOT/filename).read_text(encoding='utf-8'))
            task=next(t for t in data['task'] if t['name']=='TeamLive')
            self.assertEqual(task['entry'],'TeamLive')
            for option in task['option']:
                spec=data['option'][option]
                for source in spec.get('cases',[spec]):
                    for node in source.get('pipeline_override',{}): self.assertIn(node,team)
                if spec['type']=='input':
                    self.assertRegex(spec['inputs'][0]['default'],spec['inputs'][0]['verify'])
            interfaces.append(data)
        self.assertEqual(interfaces[0]['task'],interfaces[-1]['task'])
        self.assertEqual(interfaces[-1]['agent'],
                         {'child_exec': 'python', 'child_args': ['./agent/server.py']})

if __name__=='__main__': unittest.main()
