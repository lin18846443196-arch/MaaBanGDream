"""Challenge recovery regressions use fake input, shell and engine calls."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import Clock, AgentServer, register

with patch.object(AgentServer, "custom_action", register):
    from realtime import challenge_play as cp
from realtime import live_session as session
from realtime import team_recovery as recovery


def args(params):
    return NS(custom_action_param=json.dumps(params), task_detail=NS(task_id=42))


def prepared(difficulty="Easy", requested=None):
    return session.reset_live_run(mode="challenge", difficulty=difficulty,
        requested_difficulty=requested, prepared_for_play=True)


class ChallengePlayTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.action = cp.ChallengeProfilePlay()
        prepared()
        cp.ChallengeDisconnectJumpConfigure().run(self.context, args({}))
        self.addCleanup(patch.stopall)
        self.engine = patch.object(cp.RealtimeProfilePlay, "run").start()
        self.restart = patch.object(cp, "restart_team_game").start()
        self.discard = patch.object(cp, "discard_prearmed_backend").start()
        self.record = patch.object(cp, "append_current_run_event").start()
        self.confirm = patch.object(cp, "handle_pre_live_settings_confirm").start()

    def enable(self):
        self.assertTrue(cp.ChallengeDisconnectJumpConfigure().run(
            self.context, args({"disconnect_jump_enabled": True})))

    def run_play(self, difficulty="Easy"):
        return self.action.run(self.context, args({"difficulty": difficulty,
            "save_result_frame": True, "settings_gate_required": True}))

    def finish(self, *_):
        session.update_live_run(play_completed=True, prepared_for_play=False)
        return True

    def jump(self, *_):
        session.update_live_run(disconnect_jump_requested=True, prepared_for_play=False)
        return True

    def test_default_off_preserves_normal_completion_and_result_options(self):
        self.engine.side_effect = self.finish
        self.assertTrue(self.run_play())
        params = json.loads(self.engine.call_args.args[1].custom_action_param)
        self.assertEqual(params["run_mode"], "challenge")
        self.assertTrue(params["save_result_frame"])
        self.assertTrue(params["settings_gate_required"])
        self.assertFalse(params["life_depleted_jump_request"])
        self.assertFalse(params["defer_failed_exit"])
        self.assertNotIn("propagate_failure", params)
        self.restart.assert_not_called()

    def test_enabled_completion_does_not_restart(self):
        self.enable()
        self.engine.side_effect = self.finish
        self.assertTrue(self.run_play())
        params = json.loads(self.engine.call_args.args[1].custom_action_param)
        self.assertTrue(params["life_depleted_jump_request"])
        self.assertTrue(params["defer_failed_exit"])
        self.assertTrue(params["propagate_failure"])
        self.restart.assert_not_called()

    def test_jump_stops_failed_performance_without_counting_it(self):
        self.enable()
        self.engine.side_effect = self.jump
        order = []
        self.discard.side_effect = lambda reason: order.append("cleanup")
        self.restart.side_effect = lambda *a, **k: order.append("restart")
        self.assertFalse(self.run_play())
        self.assertEqual(order, ["cleanup", "restart"])
        flow = self.restart.call_args.args[0]
        self.assertIs(flow.context, self.context)
        self.assertEqual(flow.settings, {"difficulty": "Easy"})
        self.assertEqual(self.restart.call_args.kwargs, {"disconnect": True})
        self.assertFalse(session.current_live_run().play_completed)
        self.assertIn("\u672c\u5c40\u672a\u8ba1\u5165", cp.latest_failure_reason())

    def test_visual_life_failure_can_request_same_recovery(self):
        self.enable()
        def fail(*_):
            cp.record_failure_reason(cp.LIFE_FAILURE)
            return False
        self.engine.side_effect = fail
        self.assertFalse(self.run_play())
        self.restart.assert_called_once()

    def test_technical_failure_and_old_failure_text_never_disconnect(self):
        self.enable()
        cp.record_failure_reason(cp.LIFE_FAILURE)
        self.engine.return_value = False
        self.assertFalse(self.run_play())
        self.restart.assert_not_called()

    def test_missing_changed_or_incomplete_run_cannot_claim_success(self):
        for state in ("missing", "changed", "incomplete"):
            with self.subTest(state=state):
                before = prepared()
                def finish_bad(*_):
                    if state == "changed":
                        prepared()
                        session.update_live_run(play_completed=True)
                    return True
                self.engine.side_effect = finish_bad
                if state == "missing":
                    with patch.object(cp, "current_live_run", side_effect=[before, None]):
                        self.assertFalse(self.run_play())
                else:
                    self.assertFalse(self.run_play())
        self.restart.assert_not_called()

    def test_stale_completed_run_never_calls_engine(self):
        session.update_live_run(play_completed=True)
        self.assertFalse(self.run_play())
        self.engine.assert_not_called()

    def test_stale_jump_signal_cannot_recover_another_run(self):
        self.enable()
        def change_and_jump(*_):
            prepared()
            session.update_live_run(disconnect_jump_requested=True)
            return True
        self.engine.side_effect = change_and_jump
        self.assertFalse(self.run_play())
        self.restart.assert_not_called()

    def test_special_expert_fallback_preserves_the_confirmed_run(self):
        prepared("Expert", "Special")
        self.engine.side_effect = self.finish
        self.assertTrue(self.run_play("Special"))

    def test_other_difficulty_is_rejected_before_engine_input(self):
        self.assertFalse(self.run_play("Hard"))
        self.engine.assert_not_called()

    def test_cleanup_failure_forbids_network_or_restart_actions(self):
        self.enable()
        error = RuntimeError("Native cleanup was not confirmed")
        error.realtime_stats = NS(cleanup_failed=True, native_report={"release_confirmed": False},
            life_failed=True, life_depleted=True)
        def fail(*_):
            session.update_live_run(disconnect_jump_requested=True)
            raise error
        self.engine.side_effect = fail
        self.assertFalse(self.run_play())
        self.restart.assert_not_called()
        self.discard.assert_not_called()

    def test_clean_life_failure_exception_can_recover(self):
        self.enable()
        error = RuntimeError("life failed")
        error.realtime_stats = NS(cleanup_failed=False, native_report={"release_confirmed": True},
            life_failed=True, life_depleted=True)
        self.engine.side_effect = error
        self.assertFalse(self.run_play())
        self.restart.assert_called_once()

    def test_missing_native_release_confirmation_forbids_recovery(self):
        self.enable()
        error = RuntimeError("Native release evidence missing")
        error.realtime_stats = NS(cleanup_failed=False, native_report={},
            engine_mode="native", life_failed=True, life_depleted=True)
        self.engine.side_effect = error
        self.assertFalse(self.run_play())
        self.restart.assert_not_called()

    def test_restore_failure_is_fatal_and_never_counts_completion(self):
        self.enable()
        self.engine.side_effect = self.jump
        self.restart.side_effect = recovery.TeamRecoveryFailed("network restore not confirmed")
        self.assertFalse(self.run_play())
        self.assertIn("network restore not confirmed", cp.latest_failure_reason())
        self.assertFalse(session.current_live_run().play_completed)

    def test_stopping_during_engine_suppresses_recovery(self):
        self.enable()
        def stop(*_):
            session.update_live_run(disconnect_jump_requested=True)
            self.context.tasker.stopping = True
            return True
        self.engine.side_effect = stop
        self.assertTrue(self.run_play())
        self.restart.assert_not_called()

    def test_settings_confirmation_failure_never_starts_engine(self):
        self.confirm.side_effect = RuntimeError("settings confirmation did not disappear")
        self.assertFalse(self.run_play())
        self.engine.assert_not_called()

    def test_stop_in_settings_confirmation_never_starts_engine(self):
        def stop(*_):
            self.context.tasker.stopping = True
            raise InterruptedError("User stopped")
        self.confirm.side_effect = stop
        self.assertTrue(self.run_play())
        self.engine.assert_not_called()

    def test_new_task_configuration_resets_default_to_off(self):
        self.enable()
        self.assertTrue(cp.disconnect_jump_enabled())
        self.assertTrue(cp.ChallengeDisconnectJumpConfigure().run(self.context, args({})))
        self.assertFalse(cp.disconnect_jump_enabled())

    def test_invalid_configuration_is_rejected(self):
        for value in (1, "true", None):
            self.assertFalse(cp.ChallengeDisconnectJumpConfigure().run(
                self.context, args({"disconnect_jump_enabled": value})))


class ChallengeSettingsConfirmTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.post_click.return_value.wait.return_value = NS(succeeded=True)
        self.context = NS(tasker=NS(stopping=False, controller=self.controller),
            run_recognition=Mock(return_value=NS(hit=False)))
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch.object(cp.time, "monotonic", self.clock.time).start()
        patch.object(cp.time, "sleep", self.clock.advance).start()
        self.capture = patch.object(cp, "capture_image", side_effect=lambda context, **kwargs: object()).start()
        self.foreground = patch.object(cp, "require_game_foreground").start()

    @staticmethod
    def popup(x=700, y=550):
        return NS(hit=True, box=NS(x=x, y=y, w=100, h=40))

    def handle(self):
        return cp.handle_pre_live_settings_confirm(self.context, "Easy")

    def test_unrelated_screen_is_captured_once_without_input(self):
        self.handle()
        self.capture.assert_called_once_with(self.context, node="ChallengeRefreshScreen")
        self.assertEqual(self.context.run_recognition.call_args.args[0], "ChallengePreLiveSettingsConfirm")
        self.controller.post_click.assert_not_called()
        self.foreground.assert_not_called()

    def test_matching_popup_is_clicked_then_verified_with_fresh_frame(self):
        first, second = object(), object()
        self.capture.side_effect = [first, second]
        self.context.run_recognition.side_effect = [self.popup(), NS(hit=False)]
        self.handle()
        self.controller.post_click.assert_called_once_with(750, 570)
        self.assertIs(self.context.run_recognition.call_args_list[0].args[1], first)
        self.assertIs(self.context.run_recognition.call_args_list[1].args[1], second)
        self.assertAlmostEqual(self.clock.now, 1.75)

    def test_delayed_popup_is_checked_after_start_transition(self):
        def delayed_popup(*_):
            if self.controller.post_click.call_count:
                return NS(hit=False)
            return self.popup() if self.clock.now >= 1.5 else NS(hit=False)
        self.context.run_recognition.side_effect = delayed_popup
        self.handle()
        self.controller.post_click.assert_called_once_with(750, 570)
        self.assertAlmostEqual(self.clock.now, 1.75)
        self.assertEqual(self.capture.call_count, 2)

    def test_each_retry_uses_current_recognized_button(self):
        self.context.run_recognition.side_effect = [
            self.popup(700), self.popup(720), self.popup(740), NS(hit=False)]
        self.handle()
        self.assertEqual([call.args for call in self.controller.post_click.call_args_list],
            [(750, 570), (770, 570), (790, 570)])
        self.assertEqual(self.capture.call_count, 4)

    def test_persistent_popup_stops_after_three_clicks(self):
        self.context.run_recognition.return_value = self.popup()
        with self.assertRaisesRegex(RuntimeError, "3"):
            self.handle()
        self.assertEqual(self.controller.post_click.call_count, 3)
        self.assertEqual(self.capture.call_count, 4)
        self.assertAlmostEqual(self.clock.now, 2.25)

    def test_missing_or_unrelated_button_location_never_clicks(self):
        for result in (NS(hit=True, box=None), self.popup(50), self.popup(y=100)):
            with self.subTest(result=result):
                self.context.run_recognition.return_value = result
                with self.assertRaises(RuntimeError):
                    self.handle()
        self.controller.post_click.assert_not_called()

    def test_stop_before_capture_does_not_read_or_click(self):
        self.context.tasker.stopping = True
        with self.assertRaises(InterruptedError):
            self.handle()
        self.capture.assert_not_called()
        self.controller.post_click.assert_not_called()

    def test_stop_during_foreground_guard_never_clicks(self):
        self.context.run_recognition.return_value = self.popup()
        self.foreground.side_effect = lambda controller: setattr(self.context.tasker, "stopping", True)
        with self.assertRaises(InterruptedError):
            self.handle()
        self.controller.post_click.assert_not_called()

    def test_stop_during_initial_wait_never_captures_or_clicks(self):
        def stop(seconds):
            self.clock.advance(seconds)
            self.context.tasker.stopping = True
        with patch.object(cp.time, "sleep", side_effect=stop):
            with self.assertRaises(InterruptedError):
                self.handle()
        self.capture.assert_not_called()
        self.controller.post_click.assert_not_called()
        self.assertLessEqual(self.clock.now, 0.1)

    def test_stop_after_first_click_suppresses_second_capture_and_input(self):
        self.context.run_recognition.return_value = self.popup()
        def stop(seconds):
            self.clock.advance(seconds)
            if self.controller.post_click.call_count:
                self.context.tasker.stopping = True
        with patch.object(cp.time, "sleep", side_effect=stop):
            with self.assertRaises(InterruptedError):
                self.handle()
        self.controller.post_click.assert_called_once()
        self.capture.assert_called_once()


class ChallengeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.flow = cp.ChallengeRecovery(self.context, "Easy")
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch.object(cp.time, "monotonic", self.clock.time).start()
        patch.object(cp.time, "sleep", self.clock.advance).start()

    def test_adapter_restarts_if_root_is_unavailable_without_device_writes(self):
        gate = Mock(block=Mock(return_value=False), restore=Mock(return_value=True),
            cleanup_required=False, last_error="root unavailable")
        self.flow.recover_home = Mock()
        with patch.object(recovery, "GameNetworkGate", return_value=gate), \
             patch.object(recovery, "CooperativeLiveFlow"):
            recovery.restart_team_game(self.flow, disconnect=True)
        self.context.tasker.controller.post_stop_app.assert_called_once_with("com.bilibili.star.bili")
        self.context.tasker.controller.post_start_app.assert_called_once_with("com.bilibili.star.bili")
        self.context.tasker.controller.post_click_key.assert_not_called()
        gate.restore.assert_called_once()
        self.flow.recover_home.assert_called_once_with(just_restarted=True)

    def test_network_restore_failure_forbids_relaunch_and_home_recovery(self):
        gate = Mock(block=Mock(return_value=False), restore=Mock(return_value=False),
            cleanup_required=True, last_error="root unavailable")
        self.flow.recover_home = Mock()
        with patch.object(recovery, "GameNetworkGate", return_value=gate), \
             patch.object(recovery, "CooperativeLiveFlow"):
            with self.assertRaises(recovery.TeamRecoveryFailed):
                recovery.restart_team_game(self.flow, disconnect=True)
        self.assertEqual(gate.restore.call_count, 3)
        self.context.tasker.controller.post_start_app.assert_not_called()
        self.flow.recover_home.assert_not_called()

    def test_missing_solo_disconnect_popup_falls_back_to_restart_and_restore(self):
        gate = Mock(block=Mock(return_value=True), restore=Mock(return_value=True),
            cleanup_required=True)
        recognizer = Mock(visible=Mock(return_value=False))
        self.flow.capture = Mock()
        self.flow.recover_home = Mock()
        with patch.object(recovery, "GameNetworkGate", return_value=gate), \
             patch.object(recovery, "CooperativeLiveFlow", return_value=recognizer), \
             patch.object(recovery, "require_game_foreground"):
            recovery.restart_team_game(self.flow, disconnect=True)
        self.assertGreaterEqual(self.clock.now, 12)
        self.assertLess(self.clock.now, 16)
        gate.restore.assert_called_once()
        self.context.tasker.controller.post_stop_app.assert_called_once()
        self.assertEqual(self.context.tasker.controller.post_start_app.call_count, 2)
        self.flow.recover_home.assert_called_once()

    def test_stop_after_block_still_restores_network_and_never_relaunches(self):
        gate = Mock(block=Mock(return_value=True), restore=Mock(return_value=True),
            cleanup_required=True)
        def stop(_seconds):
            self.context.tasker.stopping = True
            raise InterruptedError("User stopped")
        self.flow.wait = stop
        self.flow.recover_home = Mock()
        with patch.object(recovery, "GameNetworkGate", return_value=gate), \
             patch.object(recovery, "CooperativeLiveFlow"), \
             patch.object(recovery, "require_game_foreground"):
            with self.assertRaises(InterruptedError):
                recovery.restart_team_game(self.flow, disconnect=True)
        gate.restore.assert_called_once()
        self.context.tasker.controller.post_start_app.assert_not_called()
        self.flow.recover_home.assert_not_called()

    def test_stop_in_wait_is_cancel_aware(self):
        def stop(seconds):
            self.clock.advance(seconds)
            self.context.tasker.stopping = True
        with patch.object(cp.time, "sleep", side_effect=stop):
            with self.assertRaises(InterruptedError):
                self.flow.wait(10)
        self.assertLessEqual(self.clock.now, 0.1)

    def test_controller_is_read_from_current_context(self):
        replacement = Mock()
        self.context.tasker.controller = replacement
        self.assertIs(self.flow.controller, replacement)

    def test_home_recovery_uses_challenge_marker_and_checks_result(self):
        with patch.object(cp.CommonRecover, "run", return_value=True) as recover:
            self.flow.recover_home(just_restarted=True)
        params = json.loads(recover.call_args.args[1].custom_action_param)
        self.assertEqual(params["home_node"], "ChallengeHomeMarker")
        self.assertEqual(params["escape_timeout_ms"], 60000)
        with patch.object(cp.CommonRecover, "run", return_value=False):
            with self.assertRaises(recovery.TeamRecoveryFailed):
                self.flow.recover_home()


if __name__ == "__main__":
    unittest.main()
