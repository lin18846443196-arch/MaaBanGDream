"""No device/network changes: real screenshots, fake controller and firewall."""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

# Reuse the one-time IPC-free agent registration in the existing suite.
from test_team_live import ROOT, ta, frame, Clock
from realtime import team_recovery as recovery
from realtime.cooperative_network import GameNetworkGate
from realtime.team_recognition import TeamRecognizer
from realtime.vision_io import imread_unicode
import screen_refresh as sr


class NetworkModalTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.context=NS(tasker=NS(stopping=False,controller=Mock()),
            run_task=Mock(return_value=NS(status=NS(succeeded=True))))
        self.flow=ta.TeamLiveFlow(self.context,ta.DEFAULT_SETTINGS)
        self.flow.wait=self.clock.advance
        self.flow.clock=self.clock.time
        self.flow.click=Mock()
        self.popup=imread_unicode(ROOT/'tests/fixtures/team/network-unavailable.png')
        self.clear=frame('012.000')
        self.addCleanup(patch.stopall)
        patch.object(ta.time,'monotonic',self.clock.time).start()

    def test_supplied_network_modal_has_correct_button_over_dimmed_team_home(self):
        for results in (False,True):
            screen=self.flow.recognizer.observe(self.popup,results=results)
            self.assertEqual(screen.state,'network_unavailable')
            self.assertEqual(screen.button,(639,528))
        self.assertIsNone(self.flow.recognizer.point(self.popup,'home_ok'))
        for path in (ROOT/'docs/team-live-recording-review').glob('frame-*.png'):
            self.assertIsNone(self.flow.recognizer.box(imread_unicode(path),'network_unavailable'),path.name)

    def test_nested_capture_dismisses_modal_once_and_returns_fresh_page(self):
        for phase in ('home',):
            with self.subTest(phase=phase):
                self.flow.phase=phase
                self.flow.network_retries=0
                self.flow.next_network_click=0
                self.flow.click.reset_mock()
                images=iter([self.popup,self.clear])
                def capture(_node):
                    self.context.tasker.controller.cached_image=next(images)
                    return NS(status=NS(succeeded=True))
                self.context.run_task.side_effect=capture
                observed=[]
                with sr.filter_captured_images(self.flow.guard_network_modal), sr.observe_captured_images(lambda img,node: observed.append(img)):
                    result=sr.capture_image(self.context,node='CommonRefreshScreen')
                self.assertIs(result,self.clear)
                self.flow.click.assert_called_once_with((639,528))
                self.assertEqual(len(observed),2)
                self.assertIsNone(sr._capture_filter.get())

    def test_preparation_or_loading_network_invalidates_pending_song(self):
        for phase in ('prepare','loading'):
            self.flow.phase=phase
            self.flow.network_failure=None
            self.flow.next_network_click=0
            with patch.object(ta,'capture_image',return_value=self.clear):
                with self.assertRaises(ta.TeamNetworkInterrupted): self.flow.guard_network_modal(self.popup)
            self.assertIn('本局身份',self.flow.network_failure)
            with self.assertRaises(ta.TeamNetworkInterrupted):
                self.flow.invoke(NS(run=Mock(return_value=True)),{},'swallowed')

    def test_native_and_intentional_disconnect_do_not_click_network_modal(self):
        for phase in ('play','recovery'):
            self.flow.phase=phase
            self.assertIs(self.flow.guard_network_modal(self.popup),self.popup)
        self.flow.click.assert_not_called()

    def test_matching_or_settings_disconnect_restarts_without_stale_page(self):
        for phase in ('entry','matching','settings','result'):
            self.flow.phase=phase
            self.flow.network_failure=None
            self.flow.network_retries=0
            self.flow.next_network_click=0
            with patch.object(ta,'capture_image',return_value=self.clear):
                with self.assertRaisesRegex(ta.TeamNetworkInterrupted,'重新从主页'):
                    self.flow.guard_network_modal(self.popup)

    def test_settings_does_not_retry_tabs_or_close_stale_dialog_on_disconnect(self):
        from realtime import game_effect_settings_action as settings
        from realtime import performance_settings_action as performance
        failure=ta.TeamNetworkInterrupted('network while reading speed')
        with patch.object(settings,'_click') as click, patch.object(settings,'_wait'), \
             patch.object(settings,'_capture',side_effect=[self.clear,failure]), \
             patch.object(performance,'_expected_speed',return_value=(5.,'test.json')), \
             patch.object(performance.time,'sleep'):
            with self.assertRaises(ta.TeamNetworkInterrupted):
                settings._run_speed_settings_from_home(self.context,params={})
        self.assertEqual([c.args[1] for c in click.call_args_list],
            [(1225,55),(755,301),performance.DEFAULT_COORDINATES['first_tab']])
        with patch.object(settings.RealtimeGameSpeedSettingsGate,'_run',side_effect=failure) as run:
            with self.assertRaises(ta.TeamNetworkInterrupted):
                settings.RealtimeGameSpeedSettingsGate().run(self.context,ta.action_args({}))
        run.assert_called_once()

    def test_repeated_network_modal_is_bounded(self):
        with patch.object(ta,'capture_image',return_value=self.popup):
            with self.assertRaisesRegex(ta.TeamNetworkInterrupted,'重试3次'):
                self.flow.guard_network_modal(self.popup)
        self.assertEqual(self.flow.click.call_count,3)
        self.assertLessEqual(self.clock.now,7)

    def test_existing_retry_popup_uses_same_global_handler(self):
        self.flow.phase='home'
        retry=frame('051.062')
        with patch.object(ta,'capture_image',return_value=self.clear):
            self.assertIs(self.flow.guard_network_modal(retry),self.clear)
        self.flow.click.assert_called_once_with((635,525))

    def test_filter_exception_propagates_and_context_is_restored(self):
        self.context.tasker.controller.cached_image=self.clear
        with sr.filter_captured_images(Mock(side_effect=RuntimeError('modal error'))):
            with self.assertRaisesRegex(RuntimeError,'modal error'): sr.capture_image(self.context)
        self.assertIsNone(sr._capture_filter.get())
        self.assertIs(sr.capture_image(self.context),self.clear)

    def test_stop_while_modal_visible_never_clicks_or_restarts(self):
        self.context.tasker.stopping=True
        with self.assertRaises(InterruptedError): self.flow.guard_network_modal(self.popup)
        self.flow.click.assert_not_called()


class EscapeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock=Clock()
        self.events=[]
        self.controller=Mock()
        self.controller.post_stop_app.side_effect=lambda package: self.job('stop-app')
        self.controller.post_start_app.side_effect=lambda package: self.job('start-app')
        self.controller.post_click_key.side_effect=lambda key: self.job('home-key')
        self.context=NS(tasker=NS(stopping=False,controller=self.controller))
        self.flow=ta.TeamLiveFlow(self.context,ta.DEFAULT_SETTINGS)
        self.flow.clock=self.clock.time
        self.flow.wait=self.wait
        self.flow.capture=Mock(return_value=object())
        self.flow.click=Mock(side_effect=lambda point:self.events.append(('click',point)))
        self.flow.recover_home=Mock(side_effect=lambda **kwargs:self.events.append('home-confirmed'))
        self.flow.log=Mock()
        self.gate=Mock(last_error='root missing',cleanup_required=False)
        self.gate.block.side_effect=lambda: self.record('block',True)
        self.gate.restore.side_effect=lambda: self.record('restore',True)
        patch.object(recovery,'GameNetworkGate',return_value=self.gate).start()
        self.recognizer=patch.object(recovery,'CooperativeLiveFlow').start().return_value
        self.recognizer.visible.return_value=True
        patch.object(recovery,'require_game_foreground').start()

    def wait(self,seconds):
        self.flow.check_stop()
        self.clock.advance(seconds)
        self.flow.check_stop()

    def record(self,value,result):
        self.events.append(value)
        return result

    def job(self,name):
        self.events.append(name)
        return NS(wait=lambda:NS(succeeded=True))

    def test_escape_confirms_two_dialogs_restores_then_restarts(self):
        recovery.restart_team_game(self.flow,disconnect=True)
        self.assertEqual(self.events,['block','home-key','start-app',
            ('click',(508,447)),('click',(754,439)),
            'stop-app','restore','start-app','home-confirmed'])
        self.assertEqual(self.flow.phase,'entry')
        self.flow.recover_home.assert_called_once_with(just_restarted=True)

    def test_missing_dialog_falls_back_to_restart_and_restores_network(self):
        self.recognizer.visible.return_value=False
        recovery.restart_team_game(self.flow,disconnect=True)
        self.assertGreaterEqual(self.clock.now,12)
        self.flow.click.assert_not_called()
        self.assertEqual(self.events[-4:],['stop-app','restore','start-app','home-confirmed'])

    def test_no_root_still_restarts_without_home_key_or_modal_click(self):
        self.gate.block.side_effect=lambda:self.record('block',False)
        recovery.restart_team_game(self.flow,disconnect=True)
        self.assertEqual(self.events,['block','stop-app','restore','start-app','home-confirmed'])
        self.flow.log.assert_any_call('未写入断网规则，无需恢复网络')
        self.assertNotIn('游戏网络已恢复',[call.args[0] for call in self.flow.log.call_args_list])

    def test_partial_network_write_logs_restore_not_noop(self):
        self.gate.block.return_value=False
        self.gate.block.side_effect=None
        self.gate.cleanup_required=True
        recovery.restart_team_game(self.flow,disconnect=True)
        self.flow.log.assert_any_call('游戏网络已恢复')

    def test_user_stop_after_block_always_restores_and_never_relaunches(self):
        def stop():
            self.context.tasker.stopping=True
            return self.record('block',True)
        self.gate.block.side_effect=stop
        with self.assertRaises(InterruptedError): recovery.restart_team_game(self.flow,disconnect=True)
        self.assertEqual(self.events,['block','restore'])

    def test_failed_restore_retries_three_times_and_prevents_relaunch(self):
        self.gate.restore.side_effect=lambda:self.record('restore',False)
        with self.assertRaisesRegex(recovery.TeamRecoveryFailed,'网络恢复未确认'):
            recovery.restart_team_game(self.flow,disconnect=True)
        self.assertEqual(self.gate.restore.call_count,3)
        self.assertEqual(self.controller.post_start_app.call_count,1) # foreground only
        self.flow.recover_home.assert_not_called()

    def test_stop_app_failure_restores_but_does_not_relaunch(self):
        self.controller.post_stop_app.return_value=None
        self.controller.post_stop_app.side_effect=lambda package:NS(wait=lambda:NS(succeeded=False))
        with self.assertRaisesRegex(recovery.TeamRecoveryFailed,'游戏已关闭'):
            recovery.restart_team_game(self.flow,disconnect=True)
        self.assertEqual(self.gate.restore.call_count,1)
        self.flow.recover_home.assert_not_called()

    def test_outside_play_restart_does_not_touch_network(self):
        recovery.restart_team_game(self.flow,disconnect=False)
        self.gate.block.assert_not_called()
        self.gate.restore.assert_not_called()
        self.assertEqual(self.events,['stop-app','start-app','home-confirmed'])

    def test_shell_uses_command_exit_status_not_successful_transport(self):
        self.controller.post_shell.return_value.wait.return_value.get.return_value='Permission denied\n__TEAM_SHELL_RC__1\n'
        code,body=recovery.maa_shell(lambda:self.controller,('iptables','-N','OUTPUT_team_test'))
        self.assertEqual(code,1)
        self.assertIn('Permission denied',body)
        self.controller.post_shell.return_value.wait.return_value.get.return_value='0\n__TEAM_SHELL_RC__0\n'
        self.assertEqual(recovery.maa_shell(lambda:self.controller,('id','-u')),(0,'0'))
        self.controller.post_shell.return_value.wait.return_value.get.return_value='truncated'
        self.assertEqual(recovery.maa_shell(lambda:self.controller,('id','-u'))[0],-1)


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.context=NS(tasker=NS(stopping=False,controller=Mock()))
        self.flow=ta.TeamLiveFlow(self.context,{**ta.DEFAULT_SETTINGS,'count':0,'max_retries':2})
        self.flow.evidence=Mock()
        self.flow.save_round_report=Mock()
        self.flow.return_home=Mock()
        self.flow.recover_home=Mock()
        self.flow.restart_after_failure=Mock()
        self.addCleanup(patch.stopall)
        patch.object(ta,'discard_prearmed_backend').start()

    def fail_play(self):
        self.flow.phase='play'
        raise RuntimeError('missed native start')

    def test_infinite_count_does_not_make_failed_song_retry_forever(self):
        self.flow.run_attempt=Mock(side_effect=self.fail_play)
        with self.assertRaisesRegex(RuntimeError,'重试已达上限'): self.flow.run()
        self.assertEqual(self.flow.run_attempt.call_count,3)
        self.assertEqual(self.flow.restart_after_failure.call_count,3)
        self.assertEqual(self.flow.completed_ids,set())
        self.flow.return_home.assert_not_called()

    def test_zero_retries_still_escapes_but_does_not_open_next_room(self):
        self.flow.settings['max_retries']=0
        self.flow.run_attempt=Mock(side_effect=self.fail_play)
        with self.assertRaisesRegex(RuntimeError,'重试已达上限'): self.flow.run()
        self.flow.run_attempt.assert_called_once()
        self.flow.restart_after_failure.assert_called_once_with('missed native start',disconnect=True)

    def test_stop_or_failed_recovery_never_reenters_or_counts_song(self):
        for error in (InterruptedError('stop'),recovery.TeamRecoveryFailed('network restore failed')):
            self.flow.run_attempt=Mock(side_effect=self.fail_play)
            self.flow.restart_after_failure=Mock(side_effect=error)
            with self.assertRaises(type(error)): self.flow.run()
            self.flow.run_attempt.assert_called_once()
            self.assertEqual(self.flow.completed_ids,set())

    def test_unconfirmed_native_release_prohibits_automatic_recovery(self):
        for stats in (NS(cleanup_failed=True,native_report={}),
                      NS(cleanup_failed=False,native_report={'release_confirmed':False})):
            error=RuntimeError('native cleanup error')
            error.realtime_stats=stats
            def fail():
                self.flow.phase='play'
                raise error
            self.flow.run_attempt=Mock(side_effect=fail)
            with self.assertRaisesRegex(recovery.TeamRecoveryFailed,'触点清理未确认'): self.flow.run()
            self.flow.run_attempt.assert_called_once()
        self.flow.restart_after_failure.assert_not_called()

    def test_settlement_network_failure_preserves_count_and_never_replays(self):
        self.flow.settings['count']=1
        run=NS(run_id='completed-before-network-error')
        self.flow.run_attempt=Mock(return_value=run)
        self.flow.return_home.side_effect=ta.TeamNetworkInterrupted('network in result')
        self.assertTrue(self.flow.run())
        self.flow.run_attempt.assert_called_once()
        self.flow.restart_after_failure.assert_called_once_with('network in result',disconnect=False)
        self.flow.recover_home.assert_not_called()
        self.flow.save_round_report.assert_called_with(run,False,'network in result')
        self.assertEqual(self.flow.completed_ids,{run.run_id})

    def test_settlement_restart_failure_saves_completion_without_new_room(self):
        run=NS(run_id='completed')
        self.flow.run_attempt=Mock(return_value=run)
        self.flow.return_home.side_effect=ta.TeamNetworkInterrupted('network')
        self.flow.restart_after_failure.side_effect=recovery.TeamRecoveryFailed('restart failed')
        with self.assertRaises(recovery.TeamRecoveryFailed): self.flow.run()
        self.flow.run_attempt.assert_called_once()
        self.assertEqual(self.flow.completed_ids,{'completed'})
        self.assertFalse(self.flow.save_round_report.call_args.kwargs['returned_home'])

    def test_life_zero_signal_is_handed_to_escape_without_settlement_inputs(self):
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        def depleted(*args):
            ta.update_live_run(disconnect_jump_requested=True,play_completed=False)
            return False
        with patch.object(ta.RealtimeProfilePlay,'run',side_effect=depleted):
            with self.assertRaisesRegex(RuntimeError,'请求断网逃生'): self.flow.play()
        self.context.tasker.controller.post_click.assert_not_called()


class SharedPlayHandoffTests(unittest.TestCase):
    def exercise_shared_play(self, *, stats=None, setup_error=None, cleanup_ok=True, engine_error=None):
        from realtime import profile_play_action as play
        from realtime.final_cover import FinalCoverResolver
        from realtime.chart_repository import LocalChartRepository
        image=imread_unicode(ROOT/'tests/fixtures/team/northern-lights-cover.png')
        resolver=FinalCoverResolver(difficulty='Expert',observed_level=26,
            observed_title='Northern lights',observed_title_confidence=.99,
            repository=LocalChartRepository(ROOT/'resource/charts'))
        resolver.observe(image)
        resolution=resolver.observe(image)
        ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        ta.update_live_run(song_level=26,song_title='Northern lights',song_title_confidence=.99,
            startup_final_cover_image=image,startup_final_cover_resolution=resolution)
        context=NS(tasker=NS(stopping=False,controller=Mock()))
        settings=NS(target_fps=60,timing_offset_ms=0,note_speed=5.,profile_path=ROOT/'profiles/test.json')
        backend=Mock()
        backend.configure_timing_offset.side_effect=setup_error
        backend.report.return_value={'release_confirmed':cleanup_ok}
        engine=Mock()
        engine.run.return_value=stats
        engine.run.side_effect=engine_error
        params=ta.team_play_params({**ta.DEFAULT_SETTINGS,'debug_recording':False,'diagnostic_trace':False})
        params['save_result_frame']=False
        params['defer_failed_exit']=True
        with ExitStack() as stack:
            stack.enter_context(patch.object(play,'resolve_profile',return_value=settings))
            stack.enter_context(patch.object(play,'verified_settings',return_value=NS(expected_note_speed=5.,actual_note_speed=5.,profile='test.json')))
            profile=stack.enter_context(patch.object(play,'RealtimeProfileStore'))
            profile.return_value.runtime_options.return_value={'native_realtime_enabled':True,'chart_prediction_enabled':True}
            stack.enter_context(patch.object(play,'debug_enabled',return_value=False))
            stack.enter_context(patch.object(play,'prepare_native_for_settings_gate',return_value=resolution.selection))
            stack.enter_context(patch.object(play,'consume_prearmed_backend',return_value=backend))
            stack.enter_context(patch.object(play,'require_game_foreground'))
            stack.enter_context(patch.object(play,'ControllerTouchDispatcher'))
            stack.enter_context(patch.object(play,'RealtimeEngine',return_value=engine))
            stack.enter_context(patch.object(play,'record_failure_reason'))
            stack.enter_context(patch.object(play.traceback,'print_exc'))
            result=play.RealtimeProfilePlay().run(context,ta.action_args(params))
        context.tasker.controller.post_click.assert_not_called()
        return result

    def test_setup_failure_carries_native_cleanup_report_to_team(self):
        with self.assertRaises(RuntimeError) as raised:
            self.exercise_shared_play(setup_error=RuntimeError('timing setup failed'),cleanup_ok=False)
        self.assertTrue(raised.exception.realtime_stats.cleanup_failed)
        self.assertIs(raised.exception.realtime_stats.native_report['release_confirmed'],False)

    def test_unconfirmed_release_blocks_even_completed_engine_before_counting(self):
        from realtime.engine import EngineStats
        with self.assertRaisesRegex(RuntimeError,'触点清理失败') as raised:
            self.exercise_shared_play(stats=EngineStats(20,10,False,completed=True,
                native_report={'release_confirmed':False}))
        self.assertFalse(ta.current_live_run().play_completed)
        self.assertIs(raised.exception.realtime_stats.native_report['release_confirmed'],False)

    def test_engine_error_retains_original_cleanup_evidence(self):
        from realtime.engine import EngineStats
        error=RuntimeError('startup rejected')
        error.realtime_stats=EngineStats(20,0,False,cleanup_failed=False,
            native_report={'release_confirmed':True})
        with self.assertRaises(RuntimeError) as raised:
            self.exercise_shared_play(engine_error=error)
        self.assertIs(raised.exception,error)
        self.assertIs(raised.exception.realtime_stats,error.realtime_stats)

    def test_error_before_engine_statistics_stops_native_and_preserves_failed_cleanup(self):
        with self.assertRaises(RuntimeError) as raised:
            self.exercise_shared_play(engine_error=RuntimeError('capture initialization failed'),cleanup_ok=False)
        self.assertTrue(raised.exception.realtime_stats.cleanup_failed)
        self.assertIs(raised.exception.realtime_stats.native_report['release_confirmed'],False)

    def test_team_propagates_original_stats_while_default_task_returns_false(self):
        from realtime import profile_play_action as play
        failure=RuntimeError('start gate rejected late start')
        failure.realtime_stats=NS(cleanup_failed=False,native_report={'release_confirmed':True})
        context=NS(tasker=NS(stopping=False))
        with patch.object(play.RealtimeProfilePlay,'_run',side_effect=failure), \
             patch.object(play,'record_failure_reason'), patch.object(play.traceback,'print_exc'):
            with self.assertRaises(RuntimeError) as raised:
                play.RealtimeProfilePlay().run(context,ta.action_args({'propagate_failure':True}))
            self.assertIs(raised.exception,failure)
            for params in ({'run_mode':'cooperative'},{'run_mode':'realtime'}):
                self.assertFalse(play.RealtimeProfilePlay().run(context,ta.action_args(params)))


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.commands=[]
        self.chain=False
        self.link=False
        self.reject_fail=False
        self.flush_fail=False
        def shell(args):
            self.commands.append(tuple(args))
            if args[0]=='dumpsys': return 0,'userId=10123'
            if args==('id','-u'): return 0,'0'
            action=args[1]
            if action=='-N': self.chain=True
            if action=='-I': self.link=True
            if action=='-A' and self.reject_fail: return 1,'no rule'
            if action=='-F' and self.flush_fail: return 1,'no flush'
            if action=='-C': return (0 if self.link else 1),'query'
            if action=='-D': self.link=False
            if action=='-X':
                if self.link: return 1,'busy'
                self.chain=False
            return 0,''
        self.gate=GameNetworkGate(shell,chain_suffix='team_test')

    def test_partial_rule_failure_is_still_cleaned(self):
        self.reject_fail=True
        self.assertFalse(self.gate.block())
        self.assertTrue(self.link)
        self.assertTrue(self.gate.restore())
        self.assertFalse(self.link)
        self.assertFalse(self.chain)

    def test_restore_failure_retains_dirty_for_finally_retry(self):
        self.assertTrue(self.gate.block())
        self.flush_fail=True
        self.assertFalse(self.gate.restore())
        self.flush_fail=False
        self.assertTrue(self.gate.restore())
        self.assertTrue(self.gate.restore())
        self.assertFalse(self.chain)

    def test_network_rules_only_target_game_uid_and_owned_chain(self):
        self.assertTrue(self.gate.block())
        self.assertTrue(self.gate.restore())
        reject=next(c for c in self.commands if '-A' in c)
        self.assertIn('--uid-owner',reject)
        self.assertIn('10123',reject)
        self.assertFalse(any('wifi' in c or 'airplane' in c for c in self.commands))


if __name__=='__main__': unittest.main()
