"""Load real Maa resources and recognize recorded images with a no-input controller."""
import json
import time
import sys
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker
from realtime.vision_io import imread_unicode
import cv2
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock
from maa.agent.agent_server import AgentServer
with patch.object(AgentServer,'custom_action',lambda name: lambda cls: cls), patch.object(AgentServer,'custom_recognition',lambda name: lambda cls: cls):
    from realtime import team_action as ta
    import common_recover as cr
    import screen_refresh as sr
import maa
from maa.library import Library
# Importing maa.agent selects its reverse-proxy DLL. This standalone replay
# owns real Resource/Tasker objects, so select the framework before creating any.
Library.open(Path(maa.__file__).parent/'bin', agent_server=False)

class NoInputController(CustomController):
    def connect(self): return True
    def request_uuid(self): return 'team-offline-recognition'
    def screencap(self): return np.zeros((720,1280,3),np.uint8)
    def refuse(self,*args): raise AssertionError('Offline validation forbids input')
    start_app=stop_app=click=swipe=touch_down=touch_move=touch_up=click_key=input_text=key_down=key_up=refuse

Tasker.set_log_dir(ROOT/'temp/team-resource-check')
resource=Resource()
assert resource.post_bundle(ROOT/'resource').wait().succeeded
controller=NoInputController()
assert controller.post_connection().wait().succeeded
tasker=Tasker()
assert tasker.bind(resource,controller)
# Measure actual Maa task overhead on a no-input controller. The old refresh
# inherited framework delays; the Team node must explicitly opt out.
refresh_timings={}
for name in ('CommonRefreshScreen','TeamRefreshScreen'):
    started=time.perf_counter()
    assert tasker.post_task(name).wait().succeeded
    refresh_timings[name]=round(time.perf_counter()-started,4)
node=resource.get_node_object('TeamRefreshScreen')
assert node.pre_delay==0 and node.post_delay==0
print('Offline refresh timings:',refresh_timings,flush=True)
checks=[('TeamHomeMarker','000.000',True),('TeamHomeLive','000.000',True),
        ('TeamLiveSelectMarker','008.000',True),('TeamEntryOCR','008.000',True),
        ('TeamPrepareTitle','035.000',True),('TeamCutInOff','035.000',True),
        ('TeamPrepareTitle','047.062',False),('TeamRoomError','035.000',False),
        ('TeamRoomError','022.000',False),('TeamHomeMarker','212.062',False)]
results=[]
for name,stamp,expected in checks:
    node=resource.get_node_object(name)
    image=imread_unicode(ROOT/f'docs/team-live-recording-review/frame-{stamp}s.png')
    job=tasker.post_recognition(node.recognition.type,node.recognition.param,image).wait()
    detail=job.get()
    hit=bool(detail and detail.nodes and detail.nodes[-1].recognition.hit)
    print(name,stamp,hit,flush=True)
    results.append(dict(node=name,frame=stamp,hit=hit,expected=expected))
    assert hit==expected,(name,detail)
(ROOT/'temp/team-resource-check/results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print('Maa resource load and 10 recorded-image recognitions passed.')

# Regression: the old marker scores 0.7339 on this actual failure scene.
current=imread_unicode(ROOT/'tests/fixtures/team/home-circle.png')
old=imread_unicode(ROOT/'resource/image/home_marker.png')
score=cv2.minMaxLoc(cv2.matchTemplate(current,old,cv2.TM_CCOEFF_NORMED))[1]
assert .72 < score < .75, score
def recognize(name,image):
    node=resource.get_node_object(name)
    detail=tasker.post_recognition(node.recognition.type,node.recognition.param,image).wait().get()
    return detail.nodes[-1].recognition

network=imread_unicode(ROOT/'tests/fixtures/team/network-unavailable.png')
for name in ('TeamHomeMarker','QuitConfirmCancel'):
    assert not recognize(name,network).hit,name
results.append(dict(check='network-popup-guards',passed=True))
print('Network popup does not match Home or quit cancellation.')

assert recognize('TeamHomeMarker',current).hit
assert recognize('TeamHomeLive',current).hit
assert recognize('TeamCutInOff',imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-off.png')).hit
assert not recognize('TeamCutInOff',imread_unicode(ROOT/'tests/fixtures/team/prepare-cutin-on.png')).hit
print('Cut in regression: gray=OFF, pink=ON confirmed with native Maa template matching.')
mode_3d=imread_unicode(ROOT/'tests/fixtures/team/prepare-3d.png')
mode_off=imread_unicode(ROOT/'tests/fixtures/team/prepare-flame-off.png')
assert recognize('TeamPrepareTitle',mode_3d).hit
assert not recognize('TeamCutInOff',mode_3d).hit
assert recognize('TeamPrepareTitle',mode_off).hit
assert recognize('TeamCutInOff',mode_off).hit
from realtime import formal_preflight as fp
current_mode=[mode_3d]
mode_controller=Mock()
def switch_mode(x,y):
    assert (x,y)==(141,649)
    current_mode[0]=mode_off
    return Mock()
mode_controller.post_click.side_effect=switch_mode
mode_context=NS(tasker=NS(stopping=False,controller=mode_controller),run_recognition=recognize)
with patch.object(fp,'capture_image',side_effect=lambda *a,**kw: current_mode[0]), patch.object(fp,'require_game_foreground'), patch.object(fp,'_wait',return_value=True):
    assert fp.RealtimeFormalPreflight().run(mode_context,ta.action_args(dict(
        page_guard='TeamPrepareTitle',cut_in_off_node='TeamCutInOff',
        check_auto_live=False,check_performance_mode=True,refresh_node='TeamRefreshScreen')))
mode_controller.post_click.assert_called_once_with(141,649)
print('Third-round 3D to OFF replay passed with native Maa page and Cut in recognition.')
reward=imread_unicode(ROOT/'tests/fixtures/team/activity-reward.png')
assert not recognize('TeamHomeMarker',reward).hit
assert not recognize('QuitConfirmCancel',reward).hit
print('Activity reward is neither homepage nor quit-cancel modal.')
rank_up=imread_unicode(ROOT/'tests/fixtures/team/character-rank-up.png')
from realtime.result_navigation import STORY_NODES
for name in ('TeamHomeMarker','QuitConfirmCancel',*STORY_NODES):
    assert not recognize(name,rank_up).hit,name
print('Recorded character rank-up is neither Home, quit confirmation nor story.')
# No false homepage hit on any non-home frame (including dimmed modals/results).
checked=0
for path in sorted((ROOT/'docs/team-live-recording-review').glob('frame-*.png')):
    seconds=float(path.stem[6:-1])
    if 7<=seconds<234:
        assert not recognize('TeamHomeMarker',imread_unicode(path)).hit,path.name
        checked+=1

# Replay startup recovery using real Maa recognitions, with all input forbidden.
replay_controller=Mock(cached_image=current)
replay_controller.post_click.side_effect=AssertionError('Unexpected click during homepage recovery')
replay_controller.post_click_key.side_effect=AssertionError('Unexpected BACK on homepage')
context=NS(tasker=NS(stopping=False,controller=replay_controller),
    run_recognition=recognize,run_task=Mock(return_value=NS(status=NS(succeeded=True))))
observed=[]
with patch.object(cr,'_prepare_game',return_value=(True,False)), patch.object(cr,'require_game_foreground'), sr.observe_captured_images(lambda image,node: observed.append(node)):
    assert cr.CommonRecover().run(context,ta.action_args(ta.RECOVERY))
assert len(observed)>=2
replay_controller.post_click_key.assert_not_called()

flow=ta.TeamLiveFlow(context,ta.DEFAULT_SETTINGS)
flow.capture=Mock(side_effect=[current,imread_unicode(ROOT/'docs/team-live-recording-review/frame-008.000s.png'),imread_unicode(ROOT/'docs/team-live-recording-review/frame-012.000s.png')])
flow.click=Mock()
flow.wait=Mock()
with patch.object(ta.time,'monotonic',side_effect=range(0,1000,3)):
    flow.navigate_team_home()
points=[call.args[0] for call in flow.click.call_args_list]
assert points==[(1170,643),(1070,324)],points
print(f'Startup regression passed: old score={score:.6f}; {checked} negative frames; recovery sends no BACK; clicks={points}')
results.append(dict(check='startup-home-regression',old_score=score,negative_frames=checked,clicks=points,passed=True))

# The stuck real image must reach shared navigation even with actual Maa guards.
# Mock only input/screen transitions; never connect to or control the emulator.
replay_controller=Mock()
context=NS(tasker=NS(stopping=False,controller=replay_controller),run_recognition=recognize)
flow=ta.TeamLiveFlow(context,ta.DEFAULT_SETTINGS)
current_result=[rank_up]
now=[0.0]
flow.capture=lambda: current_result[0]
flow.wait=lambda seconds: now.__setitem__(0,now[0]+seconds)
def close_rank_up(key):
    assert key==4
    current_result[0]=current
    return Mock()
replay_controller.post_click_key.side_effect=close_rank_up
with patch.object(ta,'require_game_foreground'), patch.object(ta.time,'monotonic',lambda: now[0]):
    assert flow.return_home()
replay_controller.post_click.assert_called_once_with(1279,719)
replay_controller.post_click_key.assert_called_once_with(4)
results.append(dict(check='rank-up-shared-result-navigation',passed=True))
results.append(dict(check='team-fast-refresh',seconds=refresh_timings,passed=True))
results.append(dict(check='third-round-3d-to-off',passed=True))
print('Rank-up replay passed with native Maa recognition; no extra input after Home.')

# September 26: cancel at title used to bypass login recognition until restart.
# Replay the real title image and home image with all controller inputs mocked.
title=imread_unicode(ROOT/'tests/fixtures/team/recovery-title.png')
assert recognize('AutoLiveLoginScreenMarker',title).hit
assert recognize('AutoLiveLoginTap',title).hit
assert not recognize('TeamHomeMarker',title).hit
replay_controller=Mock()
now=[0.0]
page=[title]
cancelled=[False]
def incident_recognition(name,image):
    if name=='QuitConfirmCancel' and not cancelled[0]:
        return NS(hit=True,box=NS(x=415,y=550,w=210,h=62))
    return recognize(name,image)
def incident_click(x,y):
    if not cancelled[0]:
        assert (x,y)==(520,581)
        cancelled[0]=True
    else:
        assert (x,y)==(640,635)
        page[0]=current
    return Mock()
def incident_wait(context,seconds):
    now[0]+=seconds
    return True
replay_controller.post_click.side_effect=incident_click
replay_controller.post_click_key.side_effect=AssertionError('Unexpected BACK during title recovery')
replay_controller.post_start_app.side_effect=AssertionError('Title recovery should not restart')
context=NS(tasker=NS(stopping=False,controller=replay_controller),run_recognition=incident_recognition)
with patch.object(cr,'_prepare_game',return_value=(True,False)), \
     patch.object(cr,'require_game_foreground'), \
     patch.object(cr,'capture_image',side_effect=lambda *a,**k:page[0]), \
     patch.object(cr,'_wait_unless_stopping',side_effect=incident_wait), \
     patch.object(cr.time,'monotonic',lambda:now[0]):
    assert cr.CommonRecover().run(context,ta.action_args(ta.RECOVERY))
assert [c.args for c in replay_controller.post_click.call_args_list]==[(520,581),(640,635)]
results.append(dict(check='cancel-title-login-replay',passed=True,elapsed_simulated_seconds=now[0]))
print('Incident title replay passed: cancel -> recognized title -> Start -> stable Home, no BACK/restart.')

# Exercise both fixed ctypes interfaces through a real in-process native
# controller. This echoes fixed test text only and cannot access ADB.
from maa_shell_compat import shell_output
class OfflineShellController(NoInputController):
    def shell(self,command,timeout):
        assert command=='offline-echo' and timeout==8000
        return 'userId=10123\n__TEAM_SHELL_RC__0\n'
shell_controller=OfflineShellController()
assert shell_controller.post_connection().wait().succeeded
assert shell_output(shell_controller,'offline-echo',8000)=='userId=10123\n__TEAM_SHELL_RC__0\n'
results.append(dict(check='native-shell-ctypes-roundtrip',passed=True))
print('Shell ctypes roundtrip passed on native no-device controller.')
(ROOT/'temp/team-resource-check/results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
