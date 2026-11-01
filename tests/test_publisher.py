"""Crash/restart contract tests for the nonblocking publisher lifecycle."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import publisher_lifecycle as lifecycle
import responder
import server


class PublicationCapabilityTests(unittest.TestCase):
    """Exercise real authority in child processes, independent of host state."""

    def setUp(self):
        import selftest

        self.temp = tempfile.TemporaryDirectory(prefix="hc-publication-control-")
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.join(self.temp.name, "home")
        self.control = os.path.join(self.temp.name, "control.json")
        selftest.write_enabled_home_fixture(self.home)
        selftest.write_control_fixture(self.control, True)
        self.env = dict(os.environ, HOME=self.home,
                        CODEX_HOME=os.path.join(self.home, ".codex"),
                        HC_CONTROL_FILE=self.control,
                        HC_RECEIPT_KEY_FILE=os.path.join(self.temp.name, "receipt-key"),
                        HC_SPOOL=os.path.join(self.temp.name, "spool"))

    def run_child(self, script):
        result = subprocess.run(
            [sys.executable, "-c", script], env=self.env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_waiting_and_nonblocking_publication_with_isolated_on_authority(self):
        # Execute every existing parity assertion, without replacing the gate.
        self.run_child("import selftest; selftest.test_publish_request()")

    def assert_waiting_publication_refused(self):
        self.run_child('''
import os
from unittest import mock
import server

assert server.capability_enabled() is False
with mock.patch.object(server, "_publish") as publish, \\
        mock.patch.object(server, "hold_palette") as palette, \\
        mock.patch.object(server, "maybe_spawn_tui") as spawn, \\
        mock.patch.object(server, "arrival_nudge") as nudge:
    result = server.run_feedback("1. Inspect the fixture", 120, "capability-test")
    assert isinstance(result, server.FeedbackError)
    assert result.structured["reason"] == "disabled-before-publication"
    publish.assert_not_called()
    palette.assert_not_called()
    spawn.assert_not_called()
    nudge.assert_not_called()
assert not os.path.exists(os.environ["HC_SPOOL"])
''')

    def test_off_authority_refuses_waiting_publication(self):
        import selftest

        selftest.write_control_fixture(self.control, False)
        self.assert_waiting_publication_refused()

    def test_on_authority_with_drifted_client_refuses_waiting_publication(self):
        os.unlink(os.path.join(self.home, ".codex", "config.toml"))
        self.assert_waiting_publication_refused()


class PublisherLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hc-publisher-")
        self.addCleanup(self.temp.cleanup)
        self.spool = self.temp.name
        import selftest
        home = os.path.join(self.temp.name, 'home')
        control = os.path.join(self.temp.name, 'control.json')
        selftest.write_enabled_home_fixture(home)
        selftest.write_control_fixture(control, True)
        env = mock.patch.dict(os.environ, {'HOME': home,
            'CODEX_HOME': os.path.join(home, '.codex'), 'HC_CONTROL_FILE': control,
            'HC_RECEIPT_KEY_FILE': os.path.join(self.temp.name, 'receipt-key')})
        env.start()
        self.addCleanup(env.stop)
        for group in ("requests", "answers"):
            os.mkdir(os.path.join(self.spool, group), 0o700)
        patch = mock.patch.object(server, "spool_dir", return_value=self.spool)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "instance_stamp", return_value={
            "session": None, "pane": None, "server_uuid": "f" * 32,
            "attached": False})
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, episode="task-a", summary="Resolve this blocker.", **extra):
        return server.publish_request(summary, 120, {"mode": "text"},
                                      client="test-producer", episode_id=episode,
                                      **extra)

    def path(self, group, rid):
        return os.path.join(self.spool, group, rid + ".json")

    def write_private(self, path, raw):
        with open(path, "wb") as stream:
            stream.write(raw)
        os.chmod(path, 0o600)

    def test_restart_reuses_one_semantic_request_and_distinct_tasks(self):
        with mock.patch.object(server, "maybe_spawn_tui") as spawn:
            first = self.request()
            spawn.assert_not_called()
        self.assertEqual(self.request(), first)
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")),
                         [first + ".json"])
        second = self.request("task-b")
        self.assertNotEqual(first, second)
        with open(self.path("requests", first)) as stream:
            self.assertNotIn("caller_process", json.load(stream))
        self.assertEqual(self.request("task-b"), second)
        with self.assertRaises(server.PublisherError):
            self.request(summary="Changed prompt.")
        with self.assertRaises(server.PublisherError):
            self.request(existing_request_id=second)

    def test_crash_between_identity_and_card_restores_original_request(self):
        real_write = lifecycle._write
        def crash_card(path, raw, *, create=False):
            if create:
                raise OSError("simulated crash before card")
            return real_write(path, raw, create=create)
        with mock.patch.object(lifecycle, "_write", side_effect=crash_card):
            with self.assertRaisesRegex(server.PublisherError, "storage unavailable"):
                self.request()
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")), [])
        rid = self.request()
        self.assertTrue(os.path.isfile(self.path("requests", rid)))
        self.assertEqual(self.request(), rid)

    def test_exact_response_survives_transport_failure_until_ack(self):
        rid = self.request()
        raw = b'{ "text" : "exact  \\u263a", "cancelled": false, "images": [] }'
        self.write_private(self.path("answers", rid), raw)
        self.assertEqual(server.published_answer("task-a", client="test-producer"),
                         raw)
        records = [name for name in os.listdir(os.path.join(self.spool, "publisher"))
                   if name.endswith(".json")]
        self.assertEqual(len(records), 1)
        record_path = os.path.join(self.spool, "publisher", records[0])
        self.assertEqual(os.stat(record_path).st_mode & 0o777, 0o600)
        self.assertTrue(os.path.exists(self.path("requests", rid)))
        self.assertTrue(os.path.exists(self.path("answers", rid)))
        os.unlink(self.path("answers", rid))  # transport loses the spool pathname
        self.assertEqual(server.published_answer("task-a", client="test-producer"),
                         raw)
        # Restore the file to exercise visible storage conflict detection.
        self.write_private(self.path("answers", rid), raw)
        digest = hashlib.sha256(raw).hexdigest()
        with mock.patch("responder.Responder.acknowledge_consumed",
                        return_value={"ok": False}):
            with self.assertRaises(server.PublisherError):
                server.acknowledge_published("task-a", digest,
                                             client="test-producer")
        self.assertEqual(server.published_answer("task-a", client="test-producer"),
                         raw)
        with self.assertRaises(server.PublisherError):
            server.acknowledge_published("task-a", "0" * 64,
                                         client="test-producer")
        self.write_private(self.path("answers", rid), b'{"text":"changed"}')
        with self.assertRaisesRegex(server.PublisherError, "storage changed"):
            server.acknowledge_published("task-a", digest,
                                         client="test-producer")
        self.write_private(self.path("answers", rid), raw)
        self.assertTrue(server.acknowledge_published("task-a", digest,
                                                      client="test-producer"))
        self.assertFalse(os.path.exists(self.path("requests", rid)))
        self.assertFalse(os.path.exists(self.path("answers", rid)))
        self.assertIsNone(server.published_answer("task-a", client="test-producer"))
        self.assertTrue(server.acknowledge_published("task-a", digest,
                                                      client="test-producer"))

    def test_responder_commit_can_be_acknowledged_after_answer_path_loss(self):
        rid = self.request()
        body = {"text": "committed reply", "cancelled": False, "images": []}
        key_path = os.path.join(self.spool, "receipt-key")
        with mock.patch.dict(os.environ, {server.RECEIPT_KEY_ENV: key_path}):
            committed = responder.Responder(self.spool).submit_local(rid, body)
            self.assertTrue(committed["ok"])
            with open(self.path("answers", rid), "rb") as stream:
                exact = stream.read()
            self.assertEqual(server.published_answer(
                "task-a", client="test-producer"), exact)
            os.unlink(self.path("answers", rid))
            self.assertEqual(server.published_answer(
                "task-a", client="test-producer"), exact)
            self.assertTrue(server.acknowledge_published(
                "task-a", hashlib.sha256(exact).hexdigest(),
                client="test-producer"))
            with open(os.path.join(
                    self.spool, "responder", "commits", rid + ".json")) as stream:
                self.assertEqual(json.load(stream)["state"], "acknowledged")
        self.assertFalse(os.path.exists(self.path("requests", rid)))
        self.assertFalse(os.path.exists(self.path("answers", rid)))
        self.assertIsNone(server.published_answer("task-a", client="test-producer"))

    def test_interrupted_cleanup_retries_without_reapplying_answer(self):
        rid = self.request()
        raw = b'{"text":"ok","cancelled":false,"images":[]}'
        self.write_private(self.path("answers", rid), raw)
        self.assertEqual(server.published_answer("task-a", client="test-producer"),
                         raw)
        digest = hashlib.sha256(raw).hexdigest()
        real_unlink = os.unlink
        def fail_request_once(path, *args, **kwargs):
            if path == self.path("requests", rid):
                raise OSError("simulated cleanup interruption")
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(lifecycle.os, "unlink", side_effect=fail_request_once):
            with self.assertRaisesRegex(server.PublisherError, "storage unavailable"):
                server.acknowledge_published("task-a", digest,
                                             client="test-producer")
        self.assertIsNone(server.published_answer("task-a", client="test-producer"))
        self.assertTrue(os.path.exists(self.path("requests", rid)))
        with mock.patch.object(lifecycle, "ARCHIVE_COUNT", 0):
            self.request("later-task")
        self.assertTrue(os.path.exists(os.path.join(
            self.spool, "publisher",
            lifecycle._key("test-producer", "task-a") + ".json")))
        self.assertTrue(server.acknowledge_published("task-a", digest,
                                                      client="test-producer"))
        self.assertFalse(os.path.exists(self.path("requests", rid)))

    def test_adopts_existing_legacy_request_and_answer_without_new_card(self):
        rid = "older-producer-request"
        old = {"id": rid, "summary": "Resolve this blocker.",
               "client": "test-producer", "created": 10, "deadline": 1000,
               "response": {"mode": "text", "options": []}}
        self.write_private(self.path("requests", rid), json.dumps(old).encode())
        raw = b'{"text":"already answered","cancelled":false,"images":[]}'
        self.write_private(self.path("answers", rid), raw)
        self.assertEqual(self.request(existing_request_id=rid), rid)
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")),
                         [rid + ".json"])
        self.assertEqual(server.published_answer("task-a", client="test-producer"),
                         raw)
        self.assertTrue(server.acknowledge_published(
            "task-a", hashlib.sha256(raw).hexdigest(), client="test-producer"))

    def test_archive_bounds_acknowledged_only(self):
        pending = self.request()
        with mock.patch.object(lifecycle, "ARCHIVE_COUNT", 4):
            for index in range(7):
                rid = self.request("done-" + str(index))
                raw = b'{"text":"ok","cancelled":false,"images":[]}'
                self.write_private(self.path("answers", rid), raw)
                self.assertEqual(server.published_answer("done-" + str(index),
                                                          client="test-producer"), raw)
                server.acknowledge_published("done-" + str(index),
                                              hashlib.sha256(raw).hexdigest(),
                                              client="test-producer")
        records = [name for name in os.listdir(os.path.join(self.spool, "publisher"))
                   if name.endswith(".json")]
        self.assertEqual(len(records), 5)
        self.assertTrue(os.path.exists(self.path("requests", pending)))

    def child(self, source, expected=0):
        env = dict(os.environ, HC_SPOOL=self.spool)
        result = subprocess.run([sys.executable, "-c", source], env=env,
                                cwd=os.path.dirname(server.__file__),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout.strip()

    def test_process_exit_at_durable_boundaries(self):
        setup = '''
import os, hashlib, json
import server, publisher_lifecycle as lifecycle
publish = lambda: server.publish_request("Resolve this blocker.", 120,
    {"mode": "text"}, client="test-producer", episode_id="task-a")
'''
        self.child(setup + '''
write = lifecycle._write
def crash(path, raw, *, create=False):
    if create:
        os._exit(71)
    return write(path, raw, create=create)
lifecycle._write = crash
publish()
''', expected=71)
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")), [])
        rid = self.child(setup + 'print(publish())')
        self.assertEqual(self.child(setup + 'print(publish())'), rid)
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")),
                         [rid + ".json"])
        raw = b'{ "text": "secret reply  \\u263a", "cancelled": false }\n'
        self.write_private(self.path("answers", rid), raw)
        # Captured in the private WAL, but no response reaches the producer.
        self.child(setup + '''
server.published_answer("task-a", client="test-producer")
os._exit(72)
''', expected=72)
        self.assertTrue(os.path.exists(self.path("answers", rid)))
        self.assertEqual(server.published_answer("task-a", client="test-producer"), raw)
        # Responder acknowledgement commits, then the producer process dies
        # before updating its own record. Retry must finish without answer loss.
        self.child(setup + '''
import responder
ack = responder.Responder.acknowledge_consumed
def crash_ack(self, request, answer, **kwargs):
    result = ack(self, request, answer, **kwargs)
    assert result["ok"]
    os._exit(73)
responder.Responder.acknowledge_consumed = crash_ack
raw = server.published_answer("task-a", client="test-producer")
server.acknowledge_published("task-a", hashlib.sha256(raw).hexdigest(),
                             client="test-producer")
''', expected=73)
        self.assertEqual(server.published_answer("task-a", client="test-producer"), raw)
        self.assertTrue(os.path.exists(self.path("answers", rid)))
        self.assertTrue(server.acknowledge_published(
            "task-a", hashlib.sha256(raw).hexdigest(), client="test-producer"))
        self.assertFalse(os.path.exists(self.path("answers", rid)))
        self.assertEqual(self.child(setup + 'print(publish())'), rid)
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")), [])

    def test_adoption_has_one_owner_and_refuses_waiting_callers(self):
        rid = self.request()
        with self.assertRaisesRegex(server.PublisherError, "already owned"):
            self.request("other-task", existing_request_id=rid)
        waiting = server._publish(
            self.spool, "Resolve this blocker.", 120,
            {"mode": "text", "options": []}, client="test-producer",
            instance=server.instance_stamp(), caller_process={"pid": 123})
        with self.assertRaisesRegex(server.PublisherError, "waiting caller"):
            self.request("other-task", existing_request_id=waiting["id"])

    def test_adopts_responder_commit_before_first_poll(self):
        rid = server.publish_request("Resolve this blocker.", 120,
                                     {"mode": "text"}, client="test-producer")
        with mock.patch.dict(os.environ, {
                server.RECEIPT_KEY_ENV: os.path.join(self.spool, "receipt-key")}):
            result = responder.Responder(self.spool).submit_local(
                rid, {"text": "answer before restart", "cancelled": False,
                      "images": []})
            self.assertTrue(result["ok"])
            with open(self.path("answers", rid), "rb") as stream:
                raw = stream.read()
            self.assertEqual(self.request(existing_request_id=rid), rid)
            self.assertEqual(server.published_answer(
                "task-a", client="test-producer"), raw)
            self.assertTrue(server.acknowledge_published(
                "task-a", hashlib.sha256(raw).hexdigest(), client="test-producer"))
        self.assertEqual(os.listdir(os.path.join(self.spool, "requests")), [])

    def test_public_errors_hide_private_exception_details(self):
        rid = self.request()
        raw = b'{"text":"private reply","cancelled":false}'
        self.write_private(self.path("answers", rid), raw)
        server.published_answer("task-a", client="test-producer")
        with mock.patch.object(responder.Responder, "acknowledge_consumed",
                               side_effect=ValueError("private reply")):
            try:
                server.acknowledge_published(
                    "task-a", hashlib.sha256(raw).hexdigest(), client="test-producer")
            except server.PublisherError:
                self.assertNotIn("private reply", traceback.format_exc())
            else:
                self.fail("adapter failure must preserve the answer")
        self.assertEqual(server.published_answer("task-a", client="test-producer"), raw)
        self.assertTrue(os.path.exists(self.path("answers", rid)))

    def test_path_import_and_contract_version(self):
        spec = importlib.util.spec_from_file_location("publisher_host", server.__file__)
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        self.assertEqual(host.PUBLISHER_CONTRACT_SCHEMA,
                         "human-copilot.publisher-lifecycle/v1")
        with mock.patch.object(host, "spool_dir", return_value=self.spool):
            rid = host.publish_request("Resolve this blocker.", 120,
                {"mode": "text"}, client="test-producer", episode_id="task-a")
            self.assertEqual(rid, self.request())

    def test_concurrent_producers_reuse_one_card(self):
        source = '''
import server
print(server.publish_request("Resolve this blocker.", 120, {"mode": "text"},
    client="test-producer", episode_id="task-a"))
'''
        children = []
        try:
            for _ in range(4):
                children.append(subprocess.Popen(
                    [sys.executable, "-c", source],
                    env=dict(os.environ, HC_SPOOL=self.spool),
                    cwd=os.path.dirname(server.__file__),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            ids = []
            for child in children:
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                ids.append(stdout.strip())
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(os.listdir(os.path.join(self.spool, "requests")),
                             [ids[0] + ".json"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_archive_byte_bound_preserves_unacknowledged_answers(self):
        rid = self.request()
        raw = b'{"text":"still unapplied"}'
        self.write_private(self.path("answers", rid), raw)
        self.assertEqual(server.published_answer("task-a", client="test-producer"), raw)
        completed = self.request("done")
        self.write_private(self.path("answers", completed), raw)
        server.published_answer("done", client="test-producer")
        with mock.patch.object(lifecycle, "ARCHIVE_BYTES", 0):
            server.acknowledge_published("done", hashlib.sha256(raw).hexdigest(),
                                        client="test-producer")
        records = [name for name in os.listdir(os.path.join(self.spool, "publisher"))
                   if name.endswith(".json")]
        self.assertEqual(records, [lifecycle._key("test-producer", "task-a") + ".json"])
        self.assertEqual(server.published_answer("task-a", client="test-producer"), raw)
        self.assertTrue(os.path.exists(self.path("answers", rid)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
