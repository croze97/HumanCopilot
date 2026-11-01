"""Overlay-only routing is a preference, never a reason to open a window."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class SurfacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = self.root / "surface.json"
        env = mock.patch.dict(os.environ, {"HC_SURFACE_POLICY_FILE": str(self.policy)})
        env.start()
        self.addCleanup(env.stop)

    def test_absent_malformed_and_unknown_preferences_are_overlay_only(self):
        self.assertEqual(server.surface_policy(), "overlay")
        for value in ("broken", "[]", '{"mode":"terminal"}', '{"mode":"overlay"}'):
            self.policy.write_text(value)
            self.assertEqual(server.surface_policy(), "overlay")

    def test_preference_changes_are_read_without_server_restart(self):
        self.policy.write_text('{"mode":"auto"}')
        self.assertEqual(server.surface_policy(), "auto")
        self.policy.write_text('{"mode":"overlay"}')
        self.assertEqual(server.surface_policy(), "overlay")

    def test_external_spawn_route_never_opens_terminal_or_pane(self):
        for attached in (True, False):
            for healthy in (True, False):
                with self.subTest(attached=attached, healthy=healthy), \
                     mock.patch.object(server, "overlay_rendering", return_value=healthy), \
                     mock.patch.object(server, "_record_delivery") as record, \
                     mock.patch.object(server, "open_in_terminal") as terminal, \
                     mock.patch.object(server, "open_pane_in_session") as pane:
                    self.assertEqual(server.maybe_spawn_tui(str(self.root), "session", {
                        "session": "session", "attached": attached}), healthy)
                    terminal.assert_not_called()
                    pane.assert_not_called()
                    self.assertEqual(record.call_args.args[3], "overlay")
                    self.assertEqual(record.call_args.args[2], "healthy" if healthy else "degraded")
        self.assertFalse((self.root / "instances").exists())

    def test_feedback_never_focuses_or_spawns_and_timeout_does_not_redirect(self):
        for attached in (True, False):
            with self.subTest(attached=attached), contextlib.ExitStack() as stack:
                for name, value in {
                    "capability_guard": contextlib.nullcontext(True),
                    "capability_enabled": True,
                    "instance_stamp": {"session": "session", "attached": attached},
                    "unattached_refusal": None,
                    "spool_dir": str(self.root),
                    "reclaim_answered_orphans": None,
                    "hold_palette": None,
                    "instance_id": "session",
                    "_publish": {"id": "test", "deadline": time.time() + .01},
                    "arrival_nudge": None,
                    "overlay_rendering": False,
                    "_record_delivery": None,
                    "clear_arrival_nudge": None,
                }.items():
                    stack.enter_context(mock.patch.object(server, name, return_value=value))
                spawn = stack.enter_context(mock.patch.object(server, "maybe_spawn_tui"))
                focus = stack.enter_context(mock.patch.object(server, "focus_pane_in_session"))
                stack.enter_context(mock.patch.object(server.responder, "Responder"))
                result = server.run_feedback("Need unavailable operator fact", 1, "test-policy")
                spawn.assert_not_called()
                focus.assert_not_called()
                text = " ".join(row.get("text", "") for row in result)
                self.assertIn("Do not repeat the unanswered request", text)
                self.assertNotIn("ask in the terminal", text)

    def test_public_publisher_refuses_disabled_without_spool_side_effects(self):
        with mock.patch.object(server, "capability_guard", return_value=contextlib.nullcontext(False)), \
             mock.patch.object(server, "spool_dir") as spool, \
             mock.patch.object(server, "_publish") as publish:
            with self.assertRaisesRegex(server.PublishRefused, "disabled"):
                server.publish_request("Need operator fact", response={"mode": "text"}, client="test")
            spool.assert_not_called()
            publish.assert_not_called()

    def test_explicit_auto_retains_legacy_route(self):
        self.policy.write_text('{"mode":"auto"}')
        with mock.patch.object(server, "open_in_terminal", return_value=["stub"]) as terminal, \
             mock.patch.object(server, "_record_delivery"):
            self.assertTrue(server.maybe_spawn_tui(str(self.root)))
            terminal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
