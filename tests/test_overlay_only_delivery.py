"""MCP prompts remain answerable without opening a console surface."""

import os
import shutil
import time
import unittest

import selftest
import server
import tui


class OverlayOnlyDelivery(unittest.TestCase):
    def setUp(self):
        self.stage = selftest._PresenceStage()

    def tearDown(self):
        for process in self.stage.servers:
            if process.proc.poll() is None:
                process.stop()
        shutil.rmtree(self.stage.spool, ignore_errors=True)

    def assert_no_console(self):
        self.assertEqual([], self.stage.terminals())
        self.assertFalse(any(" action new-pane " in line or " action focus-pane-id " in line
                             for line in self.stage.zlog()))

    def test_attached_and_unattached_prompts_use_spool_without_console(self):
        attached = self.stage.server(selftest._ZA)
        detached = self.stage.server({**selftest._ZB, "HC_STUB_CLIENTS": "0"})
        standalone = self.stage.server({})
        for process, expected_session in ((attached, "hc-A"),
                                          (detached, "hc-B"),
                                          (standalone, None)):
            request = process.call()
            self.assertEqual(expected_session, request["instance"]["session"])
            self.assert_no_console()
            iid = server.instance_id(request["instance"])
            self.assertFalse(os.path.exists(os.path.join(
                self.stage.spool, "instances", iid, "spawn.stamp")))

    def test_live_renderer_presence_is_preserved_and_drop_does_not_spawn(self):
        fd = tui.hold_presence(self.stage.spool)
        try:
            self.assertTrue(server.tui_alive(self.stage.spool, "hc-A"))
            self.assertTrue(server.tui_alive(self.stage.spool, "hc-B"))
            attached = self.stage.server(selftest._ZA)
            attached.call()
            self.assert_no_console()
        finally:
            os.close(fd)

        standalone = self.stage.server({})

        def wait_past_surface_recheck():
            time.sleep(server.SURFACE_RECHECK_S + 0.25)
            self.assert_no_console()

        standalone.call(before_answer=wait_past_surface_recheck)
        self.assert_no_console()


if __name__ == "__main__":
    unittest.main()
