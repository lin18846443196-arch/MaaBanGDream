"""Replay October 1 escape dialogs without emulator input or networking."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.vision_io import imread_unicode


FIXTURES = ROOT / "tests/fixtures/cooperative-20261001-popup"


class EscapePopupTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.flow = ca.CooperativeLiveFlow(self.context, ca.DEFAULT_SETTINGS)
        self.flow.click = Mock()
        self.continue_image = imread_unicode(FIXTURES / "continue.png")
        self.confirm_image = imread_unicode(FIXTURES / "confirm-current.png")
        self.unknown_image = imread_unicode(
            ROOT / "tests/fixtures/cooperative-20260930/restriction-24s.png"
        )
        self.addCleanup(patch.stopall)
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()

    def frames(self, images):
        images = iter(images)
        last = self.unknown_image

        def capture():
            nonlocal last
            last = next(images, last)
            return last

        self.flow.capture = capture

    def test_actual_confirm_modal_matches_current_asset(self):
        self.assertFalse(
            self.flow.visible(self.confirm_image, "disconnect_confirm_body", 0.93)
        )
        self.assertTrue(
            self.flow._escape_popup_visible(
                self.confirm_image, "disconnect_confirm_body"
            )
        )
        self.assertFalse(
            self.flow._escape_popup_visible(
                self.continue_image, "disconnect_confirm_body"
            )
        )

    def test_black_transition_does_not_match_dialogs(self):
        image = imread_unicode(FIXTURES / "black-transition.png")
        for name in ("disconnect_continue_body", "disconnect_confirm_body"):
            self.assertFalse(
                self.flow._escape_popup_visible(image, name)
            )

    def test_legacy_confirm_asset_remains_supported(self):
        image = np.full((720, 1280, 3), 255, dtype=np.uint8)
        template = self.flow.templates["disconnect_confirm_body"]
        x, y = ca.TEMPLATE_POSITIONS["disconnect_confirm_body"]
        image[y:y + template.shape[0], x:x + template.shape[1]] = template
        self.assertTrue(
            self.flow._escape_popup_visible(image, "disconnect_confirm_body")
        )

    def test_unknown_popup_is_not_clicked(self):
        self.frames([self.unknown_image])
        self.assertFalse(
            self.flow._wait_and_click(
                "disconnect_continue_body",
                ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,
                3.0,
                next_name="disconnect_confirm_body",
            )
        )
        self.flow.click.assert_not_called()

    def test_continue_click_retries_until_confirm_is_observed(self):
        self.frames(
            [self.continue_image] * 5
            + [self.unknown_image, self.confirm_image]
        )
        self.assertTrue(
            self.flow._wait_and_click(
                "disconnect_continue_body",
                ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,
                5.0,
                next_name="disconnect_confirm_body",
            )
        )
        self.assertEqual(self.flow.click.call_count, 2)
        for call in self.flow.click.call_args_list:
            self.assertEqual(call.args, (ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,))

    def test_continue_click_without_delivery_is_not_success(self):
        self.frames([self.continue_image])
        self.assertFalse(
            self.flow._wait_and_click(
                "disconnect_continue_body",
                ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,
                5.0,
                next_name="disconnect_confirm_body",
            )
        )
        self.assertEqual(self.flow.click.call_count, 3)

    def test_already_on_confirm_never_clicks_left_cancel(self):
        self.frames([self.confirm_image])
        self.assertTrue(
            self.flow._wait_and_click(
                "disconnect_continue_body",
                ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,
                3.0,
                next_name="disconnect_confirm_body",
            )
        )
        self.flow.click.assert_not_called()

    def test_confirm_click_waits_for_disappearance(self):
        self.frames([self.confirm_image] * 5 + [self.unknown_image])
        self.assertTrue(
            self.flow._wait_and_click(
                "disconnect_confirm_body",
                ca.DISCONNECT_CONFIRM_INTERRUPT_POINT,
                5.0,
                confirm_disappeared=True,
            )
        )
        self.assertEqual(self.flow.click.call_count, 2)

    def test_stuck_confirm_click_is_not_success(self):
        self.frames([self.confirm_image])
        self.assertFalse(
            self.flow._wait_and_click(
                "disconnect_confirm_body",
                ca.DISCONNECT_CONFIRM_INTERRUPT_POINT,
                5.0,
                confirm_disappeared=True,
            )
        )
        self.assertEqual(self.flow.click.call_count, 3)

    def test_full_escape_replay_uses_right_confirm_and_restores_network(self):
        self.frames(
            [self.continue_image, self.confirm_image, self.confirm_image,
             self.unknown_image]
        )
        gate = Mock()
        gate.block.return_value = True
        gate.restore.return_value = True
        self.flow.dismiss_connect_failed = Mock(return_value=True)
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            self.assertTrue(self.flow.disconnect_jump_out())
        self.assertEqual(
            [call.args for call in self.flow.click.call_args_list],
            [(ca.DISCONNECT_CONTINUE_INTERRUPT_POINT,),
             (ca.DISCONNECT_CONFIRM_INTERRUPT_POINT,)],
        )
        gate.block.assert_called_once()
        self.assertEqual(gate.restore.call_count, 2)

    def test_unknown_popup_escape_still_restores_network(self):
        self.frames([self.unknown_image])
        gate = Mock()
        gate.block.return_value = True
        gate.restore.return_value = True
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            self.assertFalse(self.flow.disconnect_jump_out(popup_timeout_s=2.0))
        gate.restore.assert_called_once()
        self.flow.click.assert_not_called()

    def test_stop_after_first_click_still_restores_network(self):
        self.frames([self.continue_image])
        self.flow.click.side_effect = lambda point: setattr(
            self.context.tasker, "stopping", True
        )
        gate = Mock()
        gate.block.return_value = True
        gate.restore.return_value = True
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            with self.assertRaises(InterruptedError):
                self.flow.disconnect_jump_out()
        gate.restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
