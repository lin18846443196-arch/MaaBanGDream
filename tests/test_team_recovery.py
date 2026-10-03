"""September 26 regression: delayed members, title cancellation and SDK Shell.

All controller inputs and clocks are simulated. No ADB or network writes.
"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from ctypes import c_char_p, c_int64

from test_team_live import ROOT, ta, Clock, frame
from realtime.team_loading import TeamLoadingBudget
from realtime.team_recognition import TeamScreen, TeamRecognizer
from realtime.vision_io import imread_unicode
from realtime import team_recovery as recovery
from realtime.cooperative_network import GameNetworkGate, resolve_game_uid
import common_recover as cr
import maa_shell_compat as compat


class LoadingBudgetTests(unittest.TestCase):
    def test_slow_members_can_take_over_sixty_seconds(self):
        budget=TeamLoadingBudget(0,180)
        for stamp in (0,30,61,120,179):
            self.assertIsNone(budget.observe(stamp,'loading',(100,45,88,80)*2+(100,100)))
        self.assertIn('180 秒无可确认进展',budget.observe(180,'loading'))

    def test_only_confirmed_forward_progress_renews_idle_timeout(self):
        budget=TeamLoadingBudget(0,180)
        low=(40,)*10
        budget.observe(0,'loading',low)
        budget.observe(1,'loading',low)
        budget.observe(100,'loading',(45,)*10)
        self.assertEqual(budget.last_progress,0)
        budget.observe(101,'loading',(45,)*10)
        self.assertEqual(budget.last_progress,101)
        for stamp,value in [(200,44),(201,44),(250,46),(251,46)]:
            budget.observe(stamp,'loading',(value,)*10)
        self.assertEqual(budget.last_progress,101)
        self.assertIn('无可确认进展',budget.observe(281,'loading',(45,)*10))

    def test_progress_never_extends_member_hard_limit(self):
        budget=TeamLoadingBudget(0,180)
        for stamp in range(360):
            self.assertIsNone(budget.observe(stamp,'loading',(10+stamp//5,)*10))
        self.assertIn('总上限 360',budget.observe(360,'loading',(90,)*10))

    def test_cover_gets_own_budget_after_slow_members(self):
        budget=TeamLoadingBudget(0,180)
        budget.observe(0,'ready_wait')
        self.assertIsNone(budget.observe(179,'loading'))
        self.assertIsNone(budget.observe(179.5,'unknown'))
        self.assertIsNone(budget.observe(230,'unknown'))
        self.assertIn('最终封面60秒超时',budget.observe(240,'unknown'))

    def test_jitter_and_unknown_progress_cannot_wait_forever(self):
        budget=TeamLoadingBudget(0,30)
        budget.observe(0,'loading')
        self.assertIn('30 秒无可确认进展',budget.observe(30,'loading'))
        budget=TeamLoadingBudget(0,30)
        for stamp in range(1,180):
            # A transient return to members cannot reset their hard budget.
            reason=budget.observe(stamp,'loading' if stamp%10==0 else 'unknown')
            if reason: break
        self.assertIsNotNone(reason)
        self.assertLessEqual(stamp,180)

    def test_preparation_without_ack_still_times_out(self):
        budget=TeamLoadingBudget(0,180)
        self.assertIn('未确认进入成员加载页',budget.observe(60,'unknown'))

    def test_recorded_failure_has_three_incomplete_members(self):
        image=imread_unicode(ROOT/'tests/fixtures/team/loading-slow-members.png')
        screen=TeamRecognizer().observe(image)
        self.assertEqual(screen.state,'loading')
        self.assertIsNotNone(screen.loading_progress)
        self.assertEqual(sum(value<97 for value in screen.loading_progress),3)
        self.assertEqual(screen.loading_progress[4],100)
        self.assertLess(abs(screen.loading_progress[2]-45),4)
        self.assertLess(abs(screen.loading_progress[5]-88),4)
        self.assertLess(abs(screen.loading_progress[6]-80),4)

    def test_slow_room_then_cover_hands_same_run_to_native(self):
        clock=Clock()
        context=NS(tasker=NS(stopping=False,controller=Mock()))
        flow=ta.TeamLiveFlow(context,ta.DEFAULT_SETTINGS)
        flow.box=Mock(return_value=None)
        flow.click=Mock()
        flow.wait=clock.advance
        flow.require_safe_preparation=Mock()
        prepare=frame('035.000')
        members=imread_unicode(ROOT/'tests/fixtures/team/loading-slow-members.png')
        cover=imread_unicode(ROOT/'tests/fixtures/team/northern-lights-cover.png')
        def capture():
            clock.advance(10)
            return prepare if clock.now<20 else members if clock.now<100 else cover
        flow.capture=capture
        run=ta.reset_live_run(mode='team',difficulty='Expert',prepared_for_play=True)
        ta.update_live_run(song_level=26,song_title='Northern lights',song_title_confidence=.99)
        with patch.object(ta.time,'monotonic',clock.time):
            flow.ready_and_wait_for_cover()
        self.assertGreater(clock.now,60)
        self.assertEqual(ta.current_live_run().run_id,run.run_id)
        self.assertEqual(ta.current_live_run().startup_final_cover_resolution.selection.bestdori_song_id,309)
        flow.click.assert_called_once_with((1123,646))


class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.page='title'
        self.inputs=[]
        self.recognized=[]
        self.controller=Mock()
        self.controller.post_click.side_effect=self.click
        self.controller.post_click_key.side_effect=lambda key:self.inputs.append(('back',self.clock.now))
        self.context=NS(tasker=NS(stopping=False,controller=self.controller),run_recognition=self.recognize)

    def click(self,x,y):
        self.inputs.append(((x,y),self.clock.now))
        self.page='title' if (x,y)==(520,581) else 'home'
        return Mock()

    def recognize(self,name,image):
        self.recognized.append(name)
        hit=((name=='QuitConfirmCancel' and image=='modal') or
             (name=='TeamHomeMarker' and image=='home') or
             (name=='AutoLiveLoginScreenMarker' and image=='title') or
             (name=='ResourceDownloadConfirm' and image=='download'))
        return NS(hit=hit,box=NS(x=415,y=550,w=210,h=62) if hit else None)

    def run_recovery(self,params=None,capture=None):
        values={**ta.RECOVERY,'restart_limit':0,'escape_timeout_ms':60000,**(params or {})}
        def wait(context,seconds):
            self.clock.advance(seconds)
            return not context.tasker.stopping
        with patch.object(cr,'_prepare_game',return_value=(True,False)), \
             patch.object(cr,'require_game_foreground'), \
             patch.object(cr,'capture_image',side_effect=capture or (lambda *a,**k:self.page)), \
             patch.object(cr,'_wait_unless_stopping',side_effect=wait), \
             patch.object(cr.time,'monotonic',self.clock.time):
            return cr.CommonRecover().run(self.context,ta.action_args(values))

    def test_cancel_at_title_resumes_login_without_restart(self):
        self.page='modal'
        self.assertTrue(self.run_recovery())
        self.assertEqual([item[0] for item in self.inputs],[(520,581),(640,635)])
        self.assertLess(self.clock.now,10)
        self.controller.post_start_app.assert_not_called()

    def test_cancel_returning_home_never_clicks_or_backs_again(self):
        self.page='modal'
        def click(x,y):
            self.inputs.append((x,y)); self.page='home'; return Mock()
        self.controller.post_click.side_effect=click
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.inputs,[(520,581)])

    def test_pending_home_on_title_is_bounded_even_without_modal(self):
        self.assertTrue(self.run_recovery({'home_confirmation_pending':True}))
        self.assertEqual([p for p,t in self.inputs],[(640,635)])

    def test_cold_start_does_not_send_back_before_title_at_25_seconds(self):
        def capture(*args,**kwargs):
            return 'boot' if self.clock.now<25 else self.page
        self.assertTrue(self.run_recovery({'app_just_started':True},capture))
        self.assertEqual([p for p,t in self.inputs],[(640,635)])
        self.assertGreaterEqual(self.inputs[0][1],25)

    def test_unresponsive_title_clicks_are_bounded_and_never_send_back(self):
        def click(x,y):
            self.inputs.append(((x,y),self.clock.now))
            return Mock()
        self.controller.post_click.side_effect=click
        self.assertFalse(self.run_recovery())
        self.assertEqual(len(self.inputs),3)
        self.assertTrue(all(p==(640,635) for p,t in self.inputs))
        self.assertLessEqual(self.clock.now,62)

    def test_home_marker_flicker_does_not_lock_unknown_page_forever(self):
        pages=iter(['home','boot'])
        def capture(*args,**kwargs): return next(pages,'title' if self.page!='home' else 'home')
        self.assertTrue(self.run_recovery(capture=capture))
        self.assertIn(((640,635),self.inputs[0][1]),self.inputs)

    def test_stop_in_pending_animation_never_clicks_title(self):
        self.page='modal'
        def capture(*args,**kwargs):
            if self.clock.now>=1.5: self.context.tasker.stopping=True
            return self.page
        self.assertTrue(self.run_recovery(capture=capture))
        self.assertEqual([p for p,t in self.inputs],[(520,581)])

    def test_download_takes_priority_over_cancel_and_title(self):
        self.page='download'
        def click(x,y): self.page='home'; return Mock()
        self.controller.post_click.side_effect=click
        self.assertTrue(self.run_recovery({'home_confirmation_pending':True}))
        self.controller.post_click.assert_called_once()
        self.controller.post_click_key.assert_not_called()

    def test_esc_recovery_can_recognize_returned_title(self):
        self.page='close'
        original=self.recognize
        def recognize(name,image):
            if name=='AutoLiveCommonClose' and image=='close':
                return NS(hit=True,box=NS(x=1,y=1,w=2,h=2))
            return original(name,image)
        self.context.run_recognition=recognize
        def click(x,y):
            self.inputs.append(((x,y),self.clock.now))
            self.page='title' if (x,y)==(2,2) else 'home'
            return Mock()
        self.controller.post_click.side_effect=click
        self.assertTrue(self.run_recovery())
        self.assertEqual([p for p,t in self.inputs],[(2,2),(640,635)])

    def test_shared_settlement_back_only_still_returns_home(self):
        self.page='reward'
        def back(key):
            self.assertEqual(key,4)
            self.page='home'
            return Mock()
        self.controller.post_click_key.side_effect=back
        self.assertTrue(self.run_recovery({'back_only':True}))
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_called_once_with(4)
        self.assertNotIn('AutoLiveLoginScreenMarker',self.recognized)

    def test_shared_settlement_restart_switches_to_login(self):
        self.page='stuck-reward'
        self.controller.post_click_key.side_effect=lambda key:Mock()
        def start(package):
            self.page='title'
            return Mock()
        self.controller.post_start_app.side_effect=start
        with patch.object(cr,'imwrite_unicode'):
            self.assertTrue(self.run_recovery({'back_only':True,'restart_limit':1,
                                               'escape_timeout_ms':4000}))
        self.controller.post_stop_app.assert_called_once()
        self.controller.post_start_app.assert_called_once()
        self.controller.post_click.assert_called_once_with(640,635)

    def test_download_in_progress_never_receives_back_or_cancel(self):
        self.page='download'
        original=self.recognize
        def recognize(name,image):
            if name=='ResourceDownloadPageMarker' and image=='downloading':
                return NS(hit=True,box=NS(x=1,y=1,w=2,h=2))
            return original(name,image)
        self.context.run_recognition=recognize
        def click(x,y):
            self.page='downloading'
            return Mock()
        self.controller.post_click.side_effect=click
        def capture(*a,**k):
            return 'home' if self.clock.now>=25 else self.page
        self.assertTrue(self.run_recovery(capture=capture))
        self.controller.post_click.assert_called_once()
        self.controller.post_click_key.assert_not_called()


class ShellDiagnosticTests(unittest.TestCase):
    def test_ctypes_shell_bindings_preserve_pointer_and_id_widths(self):
        controller=object.__new__(compat.Controller)
        controller._own=False
        controller._handle=0x123456789ab
        library=NS(MaaControllerPostShell=NS(argtypes=None,restype=None),
                   MaaControllerGetShellOutput=NS(argtypes=None,restype=None))
        with patch.object(compat.Library,'framework',return_value=library):
            compat.ensure_shell_api(controller)
        self.assertEqual(library.MaaControllerPostShell.argtypes,[compat.MaaControllerHandle,c_char_p,c_int64])
        self.assertIs(library.MaaControllerPostShell.restype,compat.MaaCtrlId)
        self.assertEqual(library.MaaControllerGetShellOutput.argtypes,
                         [compat.MaaControllerHandle,compat.MaaStringBufferHandle])

    def test_shell_exception_type_survives_uid_failure(self):
        controller=Mock()
        controller.post_shell.side_effect=OverflowError('int too long to convert')
        gate=GameNetworkGate(lambda args: recovery.maa_shell(lambda:controller,args))
        self.assertFalse(gate.block())
        self.assertIn('OverflowError',gate.last_error)
        self.assertIn('dumpsys rc=-1',gate.last_error)
        self.assertIn('pidof rc=-1',gate.last_error)
        self.assertFalse(gate.cleanup_required)
        self.assertTrue(gate.restore())

    def test_failed_shell_job_cannot_reuse_stale_success_output(self):
        controller=Mock()
        job=controller.post_shell.return_value.wait.return_value
        job.succeeded=False
        job.get.return_value='123\n__TEAM_SHELL_RC__0'
        self.assertEqual(recovery.maa_shell(lambda:controller,('pidof','game'))[0],-1)
        job.get.assert_not_called()

    def test_uid_fallback_and_invalid_pid_never_form_shell_injection(self):
        shell=Mock(side_effect=[(0,'userId=bad'),(0,'1234'),(0,'Uid:\t10123\t10123')])
        self.assertEqual(resolve_game_uid(shell),10123)
        shell=Mock(side_effect=[(1,'failed'),(0,'1234;reboot')])
        self.assertIsNone(resolve_game_uid(shell))
        self.assertEqual(shell.call_count,2)


if __name__=='__main__': unittest.main()
