"""Crash recovery tests use a simulated firewall and never contact a device."""
import shlex
import unittest

from test_team_live import ROOT
from realtime.cooperative_network import GameNetworkGate


COOP = "OUTPUT_mbdr_coop_c3958e01"
TEAM = "OUTPUT_team_aabbccddeeff"


class Firewall:
    def __init__(self):
        self.chains = []
        self.rules = []
        self.commands = []
        self.uid = 10058
        self.fail_action = None
        self.fail_read = None
        self.reads = 0
        self.keep_deleted_chain = False
        self.raw_snapshot = None

    def escape(self, name=COOP, *, uid=None, links=1, default_reject=True):
        self.chains.append(name)
        rule = ["-A", name, "-m", "owner", "--uid-owner",
                str(self.uid if uid is None else uid), "-j", "REJECT"]
        if default_reject:
            rule.extend(["--reject-with", "icmp-port-unreachable"])
        self.rules.append(rule)
        self.rules.extend([["-A", "OUTPUT", "-j", name] for _ in range(links)])

    def snapshot(self):
        return "\n".join(
            ["-P OUTPUT ACCEPT"] +
            [shlex.join(["-N", name]) for name in self.chains] +
            [shlex.join(rule) for rule in self.rules]
        )

    def __call__(self, args):
        args = tuple(args)
        self.commands.append(args)
        if args == ("id", "-u"):
            return 0, "0"
        if args[0] == "dumpsys":
            return (0, f"userId={self.uid}") if self.uid is not None else (1, "unavailable")
        if args[0] in ("pidof", "cmd"):
            return 1, "unavailable"
        if args[:2] == ("iptables", "-S"):
            self.reads += 1
            if self.reads == self.fail_read:
                return 1, "Permission denied"
            return 0, self.raw_snapshot if self.raw_snapshot is not None else self.snapshot()
        action, chain = args[1:3]
        if (action, chain) == self.fail_action:
            return 1, "write failed"
        if action == "-D":
            rule = ["-A", *args[2:]]
            if rule not in self.rules:
                return 1, "no such rule"
            self.rules.remove(rule)
        elif action == "-X":
            if any(rule[1] == chain or ("-j" in rule and rule[-1] == chain)
                   for rule in self.rules):
                return 1, "chain still referenced"
            if not self.keep_deleted_chain:
                self.chains.remove(chain)
        else:
            raise AssertionError(f"Unexpected firewall mutation: {args}")
        return 0, ""

    @property
    def writes(self):
        return [command for command in self.commands
                if command[0] == "iptables" and command[1] in ("-D", "-X", "-F")]


class StaleEscapeTests(unittest.TestCase):
    def setUp(self):
        self.firewall = Firewall()
        self.gate = GameNetworkGate(self.firewall)

    def test_empty_snapshot_succeeds_without_uid_query_or_writes(self):
        self.firewall.uid = None
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertFalse(self.firewall.writes)
        self.assertFalse(any(command[0] == "dumpsys" for command in self.firewall.commands))

    def test_permission_failure_is_not_empty_firewall(self):
        self.firewall.fail_read = 1
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertIn("Permission denied", self.gate.last_error)
        self.assertFalse(self.firewall.writes)

    def test_cooperative_and_team_rules_restore_with_duplicate_links(self):
        self.firewall.escape(links=2)
        self.firewall.escape(TEAM, default_reject=False)
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.snapshot(), "-P OUTPUT ACCEPT")
        self.assertEqual(self.firewall.reads, 2)

    def test_unrelated_rules_chains_and_comments_are_preserved(self):
        self.firewall.escape()
        self.firewall.chains.append("USER_GAME")
        others = [
            ["-A", "USER_GAME", "-m", "owner", "--uid-owner", "10059", "-j", "DROP"],
            ["-A", "OUTPUT", "-m", "comment", "--comment", COOP, "-j", "USER_GAME"],
        ]
        self.firewall.rules.extend(others)
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.chains, ["USER_GAME"])
        self.assertEqual(self.firewall.rules, others)

    def test_partial_empty_escape_chain_can_be_removed(self):
        self.firewall.chains.append(COOP)
        self.firewall.rules.append(["-A", "OUTPUT", "-j", COOP])
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.snapshot(), "-P OUTPUT ACCEPT")

    def test_unrecognized_chain_names_are_preserved(self):
        for name in ("OUTPUT_mbdr_game", "OUTPUT_mbdr_coop_bad",
                     "OUTPUT_mbdr_coop_c3958e01_extra", "OUTPUT_team_aabbccdd"):
            self.firewall.escape(name)
        before = self.firewall.snapshot()
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.snapshot(), before)
        self.assertFalse(self.firewall.writes)

    def test_other_uid_is_preserved_and_no_known_chain_is_mutated(self):
        self.firewall.escape()
        self.firewall.escape(TEAM, uid=10059)
        before = self.firewall.snapshot()
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.snapshot(), before)
        self.assertFalse(self.firewall.writes)

    def test_extra_rules_in_recognized_chain_are_preserved(self):
        self.firewall.escape()
        self.firewall.rules.append(["-A", COOP, "-j", "ACCEPT"])
        before = self.firewall.snapshot()
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.snapshot(), before)
        self.assertFalse(self.firewall.writes)

    def test_conditional_output_and_foreign_chain_references_are_preserved(self):
        for rule in (["-A", "OUTPUT", "-p", "tcp", "-j", COOP],
                     ["-A", "USER_GAME", "-j", COOP],
                     ["-A", "OUTPUT", "-g", COOP]):
            with self.subTest(rule=rule):
                firewall = Firewall()
                firewall.escape()
                firewall.rules.append(rule)
                before = firewall.snapshot()
                gate = GameNetworkGate(firewall)
                self.assertFalse(gate.restore_stale_escapes())
                self.assertEqual(firewall.snapshot(), before)
                self.assertFalse(firewall.writes)

    def test_nondefault_reject_and_uid_range_are_preserved(self):
        for field, value in ((-1, "tcp-reset"), (5, "10058-10059")):
            with self.subTest(field=field):
                firewall = Firewall()
                firewall.escape()
                firewall.rules[0][field] = value
                gate = GameNetworkGate(firewall)
                self.assertFalse(gate.restore_stale_escapes())
                self.assertFalse(firewall.writes)

    def test_uid_failure_cannot_authorize_cleanup(self):
        self.firewall.escape()
        self.firewall.uid = None
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertIn("UID", self.gate.last_error)
        self.assertFalse(self.firewall.writes)

    def test_malformed_snapshot_is_not_cleanup_authorization(self):
        self.firewall.raw_snapshot = '-N ' + COOP + '\n-A OUTPUT --comment "unterminated'
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertFalse(self.firewall.writes)

    def test_rule_write_failure_is_reported_and_next_start_can_retry(self):
        self.firewall.escape()
        self.firewall.fail_action = ("-D", "OUTPUT")
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertIn("write failed", self.gate.last_error)
        self.assertEqual(self.firewall.rules, [["-A", "OUTPUT", "-j", COOP]])
        self.firewall.fail_action = None
        self.assertTrue(self.gate.restore_stale_escapes())
        self.assertFalse(self.firewall.chains)

    def test_chain_delete_failure_is_reported(self):
        self.firewall.escape()
        self.firewall.fail_action = ("-X", COOP)
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertIn("write failed", self.gate.last_error)
        self.assertEqual(self.firewall.chains, [COOP])

    def test_permission_failure_during_verification_is_not_success(self):
        self.firewall.escape()
        self.firewall.fail_read = 2
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertIn("Permission denied", self.gate.last_error)

    def test_false_success_exit_code_cannot_hide_remaining_chain(self):
        self.firewall.escape()
        self.firewall.keep_deleted_chain = True
        self.assertFalse(self.gate.restore_stale_escapes())
        self.assertEqual(self.firewall.chains, [COOP])


if __name__ == "__main__":
    unittest.main()
