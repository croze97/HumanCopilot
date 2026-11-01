#!/usr/bin/env python3
"""Deterministic transaction tests for the transport-neutral responder."""

import json
import multiprocessing
import contextlib
import copy
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import responder
import server


class ResponderTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hc-responder-")
        self.spool = os.path.join(self.root, "spool")
        for name in ("requests", "answers", "targets"):
            os.makedirs(os.path.join(self.spool, name), mode=0o700)
        self.old_key = os.environ.get(server.RECEIPT_KEY_ENV)
        os.environ[server.RECEIPT_KEY_ENV] = os.path.join(
            self.root, "state", "receipt-key")
        self.now = 1_700_000_000.0
        self.adapter = responder.Responder(
            self.spool, clock=lambda: self.now,
            token_factory=lambda: "lease-token")

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop(server.RECEIPT_KEY_ENV, None)
        else:
            os.environ[server.RECEIPT_KEY_ENV] = self.old_key
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, path, value):
        with open(path, "w") as stream:
            json.dump(value, stream)
        os.chmod(path, 0o600)

    def request(self, request_id="req-a", *, created=None, deadline=None,
                mode="checklist"):
        summary = ("Context for the operator.\n"
                   "1. Inspect @{Firefox · top-right} → badge is green\n"
                   "2. Confirm the value")
        response = {"mode": mode, "options": []}
        if mode == "choice":
            summary = "Pick the release route."
            response["options"] = [
                {"value": "direct", "label": "Direct", "hint": "fast"},
                {"value": "relay", "label": "Private relay", "hint": None},
            ]
        elif mode in ("text", "ack", "prose"):
            summary = "Give the requested response."
        request = {
            "id": request_id, "summary": summary,
            "created": self.now if created is None else created,
            "deadline": self.now + 120 if deadline is None else deadline,
            "timeout": 120, "client": "test",
            "instance": {"session": "hc-A", "pane": "7",
                         "server_uuid": "a" * 32, "attached": True},
            "response": response,
        }
        responder.stamp_request_contract(request)
        server.stamp_checklist_contract(request, generation="generation-1")
        responder.refresh_request_revision(request)
        if request.get("checklist") is not None:
            request["request_digest"] = server.checklist_request_digest(request)
        self._write(os.path.join(self.spool, "requests", request_id + ".json"),
                    request)
        target_rows = []
        for row in request["responder"]["steps"]:
            if row.get("target") is None:
                target_rows.append({"id": row["id"], "state": "untargeted",
                                    "identity": None, "geometry": None})
            else:
                target_rows.append({
                    "id": row["id"], "state": "resolved",
                    "identity": {"kind": "mutter-window",
                                 "stable_sequence": "42", "monitor": 0},
                    "geometry": {
                        "coordinate_space":
                            "monitor-relative-physical-pixels/v1",
                        "rect": {"x": 100, "y": 20,
                                 "width": 300, "height": 200},
                        "scale": 1,
                        "monitor": {"index": 0, "logical": {
                            "x": 0, "y": 0, "width": 1920, "height": 1080}},
                    },
                })
        target = {
            "schema": responder.TARGET_SCHEMA, "schema_version": 1,
            "request_id": request_id,
            "request_revision": request["request_revision"],
            "generation": 7,
            "state": ("resolved" if any(row["state"] == "resolved"
                                         for row in target_rows)
                      else "untargeted"),
            "observed_at": self.now, "targets": target_rows,
        }
        self._write(os.path.join(self.spool, "targets", request_id + ".json"),
                    target)
        return request

    def acquire(self, **kwargs):
        return self.adapter.acquire("phone-session", 10, **kwargs)

    def test_local_choice_objection_is_incomplete_and_preserved(self):
        request = self.request(mode="choice")
        answer = {"text": "I don't know how to fix credentials", "cancelled": False,
                  "images": [], "response_completion": {
                      "schema": "human-copilot.response-completion/v1",
                      "schema_version": 1, "mode": "choice", "status": "not_done"}}
        result = self.adapter.submit_local(request["id"], answer)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["event"]["receipt"]["completion"], "not_done")
        self.assertNotEqual(result["event"]["receipt"]["schema"], server.CHECKLIST_VERDICT_SCHEMA)
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            stored = json.load(stream)
        self.assertNotIn("choice", stored)
        blocks = server.answer_content(stored, request)
        self.assertIn("NOT DONE", blocks[0]["text"])
        self.assertEqual(blocks[1]["text"], answer["text"])
        self.assertTrue(self.adapter.acknowledge_consumed(request, stored)["ok"])

    def test_local_incomplete_choice_rejects_blank_or_conflicting_selection(self):
        request = self.request(mode="choice")
        marker = {"schema": "human-copilot.response-completion/v1", "schema_version": 1,
                  "mode": "choice", "status": "not_done"}
        for extra in ({"text": " "}, {"choice": {"value": "direct"}},
                      {"response_completion": {**marker, "status": "passed"}}):
            answer = {"text": "cannot do this", "cancelled": False, "images": [],
                      "response_completion": marker, **extra}
            result = self.adapter.submit_local(request["id"], answer)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["event"]["type"], "malformed")
        self.assertFalse(os.path.exists(os.path.join(self.spool, "answers", request["id"] + ".json")))

    def test_empty_resident_checklist_allows_only_local_prose_or_decline(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                request = self.request("empty-" + str(cancelled))
                request["summary"] = "The GPS Track test app is ready."
                request.pop("checklist", None)
                responder.stamp_request_contract(request)
                server.stamp_checklist_contract(request, generation="generation-1")
                responder.refresh_request_revision(request)
                request["request_digest"] = server.checklist_request_digest(request)
                path = os.path.join(self.spool, "requests", request["id"] + ".json")
                self._write(path, request)
                self.assertTrue(self.adapter.local_available(request))
                with self.assertRaises(responder.AdapterError):
                    self.adapter._load_request(request["id"])
                structured = self.adapter.submit_local(request["id"], {
                    "text": "", "images": [], "cancelled": False,
                    "checklist": {"steps": []}})
                self.assertFalse(structured["ok"])
                answer = {"text": "" if cancelled else "There are no steps to check.",
                          "images": [], "cancelled": cancelled}
                result = self.adapter.submit_local(request["id"], answer)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["event"]["compatibility"])
                self.assertIsNone(result["event"]["receipt"])
                saved = self.adapter._read(os.path.join(
                    self.spool, "answers", request["id"] + ".json"))
                self.assertEqual(saved, answer)
                self.assertEqual(self.adapter._read(path), request)
                self.assertTrue(self.adapter.acknowledge_consumed(request, saved)["ok"])
                second = self.adapter.submit_local(request["id"], {
                    **answer, "text": "must not overwrite"})
                self.assertFalse(second["ok"])
                self.assertEqual(self.adapter._read(os.path.join(
                    self.spool, "answers", request["id"] + ".json")), answer)

    @staticmethod
    def checklist_answer(request, verdict="pass"):
        return {
            "schema": responder.ANSWER_SCHEMA, "schema_version": 1,
            "mode": "checklist", "note": "checked on the phone",
            "steps": [{"id": row["id"], "verdict": verdict, "note": ""}
                      for row in request["responder"]["steps"]],
        }

    def test_steady_state_ensure_does_not_touch_directory_attributes(self):
        # host-oq7.6.1: chmod on every run raised IN_ATTRIB on requests/, which
        # the overlay answered with another --one-shot spawn, forever.
        self.adapter._ensure()
        with mock.patch.object(responder.os, "chmod") as chmod:
            self.adapter._ensure()
        chmod.assert_not_called()
        drifted = os.path.join(self.spool, "requests")
        os.chmod(drifted, 0o755)
        self.adapter._ensure()
        self.assertEqual(stat.S_IMODE(os.stat(drifted).st_mode), 0o700)

    def test_server_stamp_and_exact_fifo(self):
        later_id = self.request("req-z", created=self.now)
        first = self.request("req-a", created=self.now)
        result = self.acquire()
        self.assertTrue(result["ok"])
        envelope = result["event"]["request"]
        self.assertEqual(envelope["id"], "req-a")
        self.assertEqual(envelope["queue"], {"position": 1, "count": 2})
        self.assertEqual(envelope["revision"], first["request_revision"])
        self.assertEqual(envelope["instance"], first["instance"])
        self.assertEqual(envelope["provider"], first["provider"])
        self.assertEqual(
            envelope["response"]["steps"][0], {
                "id": "step-001", "index": 1, "instruction": "Inspect",
                "observation": "badge is green",
                "target": {"window": "Firefox", "region": "top-right",
                           "rect": None}})
        self.assertEqual(later_id["id"], "req-z")

    def test_canonical_contract_matches_surface_indent_inline_and_table_grammar(self):
        summary = (
            "  1. **alpha** [docs](https://example.test) "
            "@{Firefox · top-right}   \n"
            "      1. nested body, not a top-level step\n"
            "  2) [ ] `beta`\t")
        request = {
            "id": "canonical", "summary": summary, "created": self.now,
            "deadline": self.now + 60, "response": {"mode": None,
                                                       "options": []},
        }
        contract = responder.stamp_request_contract(request)
        self.assertEqual(contract["mode"], "checklist")
        self.assertEqual(
            [row["instruction"] for row in contract["steps"]],
            ["alpha docs (https://example.test)", "beta"])
        self.assertEqual(contract["steps"][0]["target"], {
            "window": "Firefox", "region": "top-right", "rect": None})

        table = responder.canonical_prompt(
            "| # | do | report |\n"
            "|---|---|---|\n"
            "| 1 | **click** → toast | report [pass](https://example.test) |")
        self.assertEqual(table["context"], "")
        self.assertEqual(table["steps"], [{
            "instruction": "click · report pass (https://example.test)",
            "observation": "toast", "target": None}])

    def test_malformed_file_blocks_fifo(self):
        self.request("req-z", created=self.now + 1)
        path = os.path.join(self.spool, "requests", "bad.json")
        with open(path, "w") as stream:
            stream.write("{")
        result = self.acquire()
        self.assertFalse(result["ok"])
        self.assertIn(result["event"]["type"], ("malformed", "malformed-state"))

    def test_lease_blocks_local_and_never_skips_oldest(self):
        request = self.request("req-a", created=self.now)
        self.request("req-z", created=self.now + 1)
        lease = self.acquire()
        conflict = self.adapter.acquire("another-phone", 10)
        self.assertEqual(conflict["event"]["type"], "conflict")
        local = self.adapter.submit_local(
            request["id"], {"text": "local", "cancelled": False, "images": []})
        self.assertEqual(local["event"]["type"], "conflict")
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))
        self.assertEqual(lease["event"]["request"]["id"], "req-a")

    def test_local_decline_remains_valid_for_every_response_mode(self):
        for index, mode in enumerate(("checklist", "choice", "text", "ack")):
            with self.subTest(mode=mode):
                request = self.request(
                    f"decline-{mode}", created=self.now + index, mode=mode)
                answer = {"text": "", "cancelled": True, "images": []}
                result = self.adapter.submit_local(request["id"], answer)
                self.assertTrue(result["ok"], result)
                with open(os.path.join(
                        self.spool, "answers", request["id"] + ".json")) as stream:
                    persisted = json.load(stream)
                self.assertTrue(persisted["cancelled"])
                self.assertNotIn("choice", persisted)

    def test_remote_checklist_commit_receipt_and_reconnect(self):
        request = self.request()
        leased = self.acquire()
        token = leased["event"]["request"]["lease"]["token"]
        args = dict(request_id=request["id"],
                    revision=request["request_revision"], lease_token=token,
                    geometry_generation=7, submission_id="submission-1",
                    answer=self.checklist_answer(request))
        committed = self.adapter.submit(**args)
        self.assertEqual(committed["event"]["type"], "committed")
        receipt = committed["event"]["receipt"]
        commit_receipt = committed["event"]["commit_receipt"]
        expected = dict(request=leased["event"]["request"],
                        geometry_generation=7, submission_id="submission-1",
                        answer=args["answer"], lease_token=token)
        self.assertEqual(expected["request"]["request_digest"], request["request_digest"])
        self.assertTrue(responder.verify_remote_receipt(
            receipt, commit_receipt=commit_receipt, **expected)["valid"])
        self.assertEqual(receipt["schema"], server.CHECKLIST_VERDICT_SCHEMA)
        self.assertTrue(receipt["authorizing"])
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        with open(answer_path) as stream:
            answer = json.load(stream)
        self.assertEqual(answer["receipt"], receipt)
        ack = self.adapter.acknowledge_consumed(request, answer)
        self.assertTrue(ack["ok"])
        os.unlink(answer_path)
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        replay = self.adapter.submit(**args)
        self.assertEqual(replay["event"]["type"], "idempotent")
        self.assertEqual(replay["event"]["receipt"], receipt)
        self.assertEqual(replay["event"]["commit_receipt"], commit_receipt)
        changed = dict(args)
        changed["submission_id"] = "submission-2"
        self.assertEqual(self.adapter.submit(**changed)["event"]["type"],
                         "already-answered")

    def test_submit_after_a_recorded_cancellation_reports_the_cancellation(self):
        # A cancellation that beats a submit unlinks the lease. Reporting the
        # bare lease conflict told the consumer to retry a request that is
        # already over (the phone bridge mapped it to a held lease); the
        # terminal record outlives the lease and names the real outcome.
        request = self.request()
        acquired = self.acquire()["event"]["request"]
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"],
                    geometry_generation=7, submission_id="submission-1",
                    answer=self.checklist_answer(request))
        terminal = self.adapter.record_terminal(
            request, "cancelled", "caller cancelled")
        self.assertTrue(terminal["ok"], terminal)
        result = self.adapter.submit(**args)
        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["type"], "cancelled")
        self.assertEqual(result["event"]["detail"], "caller cancelled")
        self.assertEqual(result["event"]["revision"], request["request_revision"])
        # Fail-closed: no answer, and no commit record to replay against.
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))
        self.assertIsNone(self.adapter._read(
            self.adapter._path("commits", request["id"])))
        # An ordinary lost lease with no terminal record is still a conflict.
        other = self.request("req-b", created=self.now + 1)
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        leased = self.acquire()["event"]["request"]
        os.unlink(self.adapter._path("leases", other["id"]))
        stranded = self.adapter.submit(
            request_id=other["id"], revision=leased["revision"],
            lease_token=leased["lease"]["token"], geometry_generation=7,
            submission_id="submission-2", answer=self.checklist_answer(other))
        self.assertEqual(stranded["event"]["type"], "conflict")

    def test_remote_receipt_refuses_changed_outbound_binding_or_missing_context(self):
        request = self.request()
        acquired = self.acquire()["event"]["request"]
        answer = self.checklist_answer(request)
        expected = dict(request=acquired, geometry_generation=7,
                        submission_id="bound-1", answer=answer,
                        lease_token=acquired["lease"]["token"])
        result = self.adapter.submit(request_id=request["id"],
            revision=acquired["revision"], **{k: v for k, v in expected.items() if k != "request"})
        receipt, binding = result["event"]["receipt"], result["event"]["commit_receipt"]
        mutations = [dict(expected, lease_token="different-token"),
                     dict(expected, geometry_generation=8),
                     dict(expected, submission_id="bound-2"),
                     dict(expected, answer={**answer, "note": "different"})]
        for field in ("request_digest", "provider", "revision"):
            changed = copy.deepcopy(expected)
            changed["request"].pop(field)
            mutations.append(changed)
        changed = copy.deepcopy(expected)
        changed["request"]["provider"]["generation"] = "other-generation"
        mutations.append(changed)
        for mutation in mutations:
            self.assertFalse(responder.verify_remote_receipt(
                receipt, commit_receipt=binding, **mutation)["valid"])
        self.assertFalse(responder.verify_remote_receipt(receipt, commit_receipt=None, **expected)["valid"])
        tampered = copy.deepcopy(binding)
        tampered["submission"]["id"] = "bound-2"
        self.assertFalse(responder.verify_remote_receipt(receipt, commit_receipt=tampered, **expected)["valid"])

    def _check_non_authorizing_remote_receipt(self, verdict):
        request = self.request()
        acquired = self.acquire()["event"]["request"]
        answer = self.checklist_answer(request, verdict)
        expected = dict(request=acquired, geometry_generation=7,
                        submission_id="negative-1", answer=answer,
                        lease_token=acquired["lease"]["token"])
        event = self.adapter.submit(request_id=request["id"], revision=acquired["revision"],
            **{k: v for k, v in expected.items() if k != "request"})["event"]
        verified = responder.verify_remote_receipt(event["receipt"],
            commit_receipt=event["commit_receipt"], **expected)
        self.assertTrue(verified["valid"])
        self.assertFalse(verified["authorizing"])
        # A separately authentic positive verdict for the same request cannot
        # replace the exact negative answer bound by this transaction.
        other_answer = self.adapter._validate_remote_answer(request, self.checklist_answer(request))
        other_receipt = server.build_checklist_receipt(request, other_answer, answered_at=self.now)
        wrong = responder.verify_remote_receipt(other_receipt,
            commit_receipt=event["commit_receipt"], **expected)
        self.assertFalse(wrong["valid"])
        self.assertFalse(wrong["authorizing"])

    def test_remote_failed_receipt_is_valid_but_cannot_authorize(self):
        self._check_non_authorizing_remote_receipt("fail")

    def test_remote_not_done_receipt_is_valid_but_cannot_authorize(self):
        self._check_non_authorizing_remote_receipt("not_done")

    def test_prepared_remote_recovery_retains_signed_commit_binding(self):
        request = self.request()
        acquired = self.acquire()["event"]["request"]
        answer = self.checklist_answer(request)
        def crash(boundary):
            if boundary == "after-prepare":
                raise RuntimeError("synthetic interruption")
        interrupted = responder.Responder(self.spool, clock=lambda: self.now, fault_hook=crash)
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    geometry_generation=7, submission_id="recovery-1", answer=answer,
                    lease_token=acquired["lease"]["token"])
        self.assertFalse(interrupted.submit(**args)["ok"])
        recovered = self.adapter.submit(**args)["event"]
        self.assertTrue(responder.verify_remote_receipt(recovered["receipt"],
            commit_receipt=recovered["commit_receipt"], request=acquired,
            **{k: v for k, v in args.items() if k not in ("request_id", "revision")})["valid"])
        self.assertEqual(self.adapter.submit(**args)["event"]["commit_receipt"], recovered["commit_receipt"])

    def test_write_ahead_crash_recovers_without_pixels(self):
        request = self.request()
        leased = self.acquire()
        token = leased["event"]["request"]["lease"]["token"]
        boundaries = []

        def crash(boundary):
            boundaries.append(boundary)
            if boundary == "after-prepare":
                raise RuntimeError("simulated interruption")

        crashing = responder.Responder(
            self.spool, clock=lambda: self.now,
            token_factory=lambda: "unused", fault_hook=crash)
        args = dict(request_id=request["id"],
                    revision=request["request_revision"], lease_token=token,
                    geometry_generation=7, submission_id="submission-wal",
                    answer=self.checklist_answer(request, "not_done"))
        failed = crashing.submit(**args)
        self.assertEqual(failed["event"]["type"], "unavailable")
        self.assertEqual(boundaries, ["after-prepare"])
        commit_path = os.path.join(self.spool, "responder", "commits",
                                   request["id"] + ".json")
        with open(commit_path) as stream:
            record_text = stream.read()
        def fields(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key.lower().replace("_", "")
                    yield from fields(child)
            elif isinstance(value, list):
                for child in value:
                    yield from fields(child)
        # Digest hex can randomly contain "b64". The privacy contract denies
        # image-bearing fields at every depth, not arbitrary hash substrings.
        self.assertTrue({"image", "images", "b64", "base64", "capture", "pixels",
                         "screenshot", "frame"}.isdisjoint(fields(json.loads(record_text))))
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))
        competing = self.adapter.submit(**{**args,
            "submission_id": "different-remote-submission"})
        self.assertEqual(competing["event"]["type"], "conflict")
        self.assertEqual(competing["event"]["detail"], "commit-prepared")
        recovered = self.adapter.submit(**args)
        self.assertEqual(recovered["event"]["type"], "committed")
        self.assertFalse(recovered["event"]["receipt"]["authorizing"])

    def test_local_prepared_crash_reserves_fifo_then_restart_replaces_safely(self):
        request = self.request(mode="text")
        first_answer = {
            "text": "first draft", "cancelled": False,
            "images": [{"mime": "image/png", "b64": "lost-pixels"}],
        }

        def crash(boundary):
            if boundary == "after-prepare":
                raise RuntimeError("local helper interrupted")

        crashing = responder.Responder(
            self.spool, clock=lambda: self.now, fault_hook=crash)
        interrupted = crashing.submit_local(
            request["id"], first_answer, submission_id="local-before-restart")
        self.assertEqual(interrupted["event"]["type"], "unavailable")
        commit_path = os.path.join(
            self.spool, "responder", "commits", request["id"] + ".json")
        with open(commit_path) as stream:
            prepared_text = stream.read()
        prepared = json.loads(prepared_text)
        self.assertEqual((prepared["state"], prepared["origin"]),
                         ("prepared", "local"))
        self.assertNotIn("lost-pixels", prepared_text)
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))

        reserved = self.acquire()
        self.assertEqual(reserved["event"]["type"], "conflict")
        self.assertEqual(reserved["event"]["detail"], "commit-prepared")
        lease_path = os.path.join(
            self.spool, "responder", "leases", request["id"] + ".json")
        self.assertFalse(os.path.exists(lease_path))

        # Simulate a lease record left by an older implementation/race. The
        # replacement rechecks it under the same flock before superseding.
        token = "remote-race-token"
        self.adapter._write(lease_path, {
            "schema": responder.LEASE_SCHEMA, "schema_version": 1,
            "request_id": request["id"],
            "request_revision": request["request_revision"],
            "token_digest": self.adapter._token_digest(token),
            "consumer_digest": self.adapter._consumer_digest("remote"),
            "acquired_at": self.now, "expires_at": self.now + 20,
        })
        replacement_answer = {
            "text": "restored draft", "cancelled": False, "images": []}
        blocked = self.adapter.submit_local(
            request["id"], replacement_answer,
            submission_id="local-after-restart")
        self.assertEqual(blocked["event"]["type"], "conflict")
        with open(commit_path) as stream:
            self.assertEqual(json.load(stream)["submission_id"],
                             "local-before-restart")

        os.unlink(lease_path)
        replaced = self.adapter.submit_local(
            request["id"], replacement_answer,
            submission_id="local-after-restart")
        self.assertEqual(replaced["event"]["type"], "committed")
        with open(os.path.join(
                self.spool, "answers", request["id"] + ".json")) as stream:
            self.assertEqual(json.load(stream)["text"], "restored draft")
        with open(commit_path) as stream:
            final_record = json.load(stream)
        self.assertEqual((final_record["state"], final_record["origin"]),
                         ("published", "local"))

    def test_after_publish_interruption_and_immediate_consumption_replay(self):
        request = self.request(mode="text")
        leased = self.acquire()["event"]["request"]["lease"]
        answer = {"schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                  "mode": "text", "text": "exact answer", "note": ""}

        def crash(boundary):
            if boundary == "after-publish":
                raise RuntimeError("server won the scheduling race")

        adapter = responder.Responder(
            self.spool, clock=lambda: self.now, fault_hook=crash)
        args = dict(request_id=request["id"],
                    revision=request["request_revision"],
                    lease_token=leased["token"], geometry_generation=7,
                    submission_id="after-publish", answer=answer)
        interrupted = adapter.submit(**args)
        self.assertEqual(interrupted["event"]["type"], "unavailable")
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        with open(answer_path) as stream:
            visible = json.load(stream)
        receipt = visible["receipt"]
        acknowledged = self.adapter.acknowledge_consumed(request, visible)
        self.assertTrue(acknowledged["ok"])
        os.unlink(answer_path)
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        replay = self.adapter.submit(**args)
        self.assertEqual(replay["event"]["type"], "idempotent")
        self.assertEqual(replay["event"]["receipt"], receipt)

    def test_choice_receipt_is_signed_non_authorizing_and_images_rejected(self):
        request = self.request(mode="choice")
        leased = self.acquire()
        token = leased["event"]["request"]["lease"]["token"]
        base = {
            "schema": responder.ANSWER_SCHEMA, "schema_version": 1,
            "mode": "choice", "choices": ["direct"], "note": "",
        }
        args = dict(request_id=request["id"],
                    revision=request["request_revision"], lease_token=token,
                    geometry_generation=7, submission_id="choice-1", answer=base)
        denied_cardinality = self.adapter.submit(**{
            **args, "submission_id": "choice-empty",
            "answer": {**base, "choices": []}})
        self.assertEqual(denied_cardinality["event"]["type"], "malformed")
        result = self.adapter.submit(**args)
        receipt = result["event"]["receipt"]
        expected_submission = responder.submission_digest(
            request["id"], request["request_revision"], 7, "choice-1", base,
            lease_token=token)
        expected_answer = responder.answer_digest(base)
        self.assertEqual(result["event"]["submission_digest"],
                         expected_submission)
        self.assertEqual(result["event"]["answer_digest"], expected_answer)
        verified = responder.verify_commit_receipt(
            receipt, expected_request_id=request["id"],
            expected_request_revision=request["request_revision"],
            expected_submission_id="choice-1",
            expected_submission_digest=expected_submission,
            expected_answer_digest=expected_answer)
        self.assertTrue(verified["valid"])
        self.assertFalse(verified["authorizing"])
        wrong = responder.verify_commit_receipt(
            receipt, expected_request_id=request["id"],
            expected_request_revision=request["request_revision"],
            expected_submission_id="choice-1",
            expected_submission_digest=expected_submission,
            expected_answer_digest=responder.answer_digest({**base, "note": "other"}))
        self.assertFalse(wrong["valid"])
        self.assertEqual(wrong["reason"], "wrong-binding")

        request2 = self.request("req-image", created=self.now + 1, mode="choice")
        # release/consume the first queue entry before the second can lease
        with open(os.path.join(self.spool, "answers", "req-a.json")) as stream:
            answer = json.load(stream)
        self.adapter.acknowledge_consumed(request, answer)
        os.unlink(os.path.join(self.spool, "answers", "req-a.json"))
        os.unlink(os.path.join(self.spool, "requests", "req-a.json"))
        leased2 = self.acquire()
        bad = dict(base)
        bad["images"] = [{"b64": "pixels"}]
        denied = self.adapter.submit(
            request_id=request2["id"], revision=request2["request_revision"],
            lease_token=leased2["event"]["request"]["lease"]["token"],
            geometry_generation=7, submission_id="choice-image", answer=bad)
        self.assertEqual(denied["event"]["type"], "malformed")

    def test_concurrent_local_publication_never_overwrites(self):
        request = self.request(mode="text")
        barrier = threading.Barrier(3)
        results = []

        def submit(text):
            barrier.wait()
            results.append(self.adapter.submit_local(
                request["id"], {"text": text, "cancelled": False, "images": []},
                submission_id="local-" + text))

        threads = [threading.Thread(target=submit, args=(value,))
                   for value in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(row["ok"] for row in results), [False, True])
        with open(os.path.join(
                self.spool, "answers", request["id"] + ".json")) as stream:
            winner = json.load(stream)
        self.assertIn(winner["text"], ("one", "two"))
        loser = "two" if winner["text"] == "one" else "one"
        self.assertNotEqual(winner["text"], loser)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(
            self.spool, "answers", request["id"] + ".json")).st_mode), 0o600)

    def test_terminal_events_emit_once_without_pinning_and_resume_is_exact(self):
        expired = self.request("req-a", created=self.now - 10,
                               deadline=self.now - 1, mode="text")
        later = self.request("req-z", created=self.now + 1, mode="text")
        first = self.acquire()
        self.assertEqual(first["event"]["type"], "expired")
        explicit = self.acquire(resume={
            "request_id": expired["id"],
            "revision": expired["request_revision"], "token": "old-token"})
        self.assertEqual(explicit["event"]["type"], "expired")
        second = self.acquire()
        self.assertTrue(second["ok"])
        self.assertEqual(second["event"]["request"]["id"], later["id"])

    def test_surface_expiry_tombstones_request_and_rejects_late_answer(self):
        request = self.request("expired-surface", created=self.now - 10,
                               deadline=self.now - 1, mode="text")
        expired = self.adapter.expire(
            request["id"], request["request_revision"])
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["event"]["type"], "expired")
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "requests", request["id"] + ".json")))
        terminal = self.adapter._read(
            self.adapter._path("terminal", request["id"]))
        self.assertEqual(terminal["event"], "expired")

        late = self.adapter.submit_local(request["id"], {
            "text": "typed after the card expired", "cancelled": False,
            "images": []}, submission_id="late-surface-answer")
        self.assertFalse(late["ok"])
        self.assertEqual(late["event"]["type"], "expired")
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))

        again = self.adapter.expire(request["id"], request["request_revision"])
        self.assertEqual(again["event"]["type"], "expired")

    def test_surface_expiry_does_not_retire_a_live_request_or_prepared_wal(self):
        live = self.request("live-surface", deadline=self.now + 30, mode="text")
        not_yet = self.adapter.expire(live["id"], live["request_revision"])
        self.assertEqual(not_yet["event"]["type"], "not-expired")
        self.assertTrue(os.path.exists(os.path.join(
            self.spool, "requests", live["id"] + ".json")))

        prepared = self.request("prepared-surface", created=self.now - 10,
                                deadline=self.now - 1, mode="text")
        self.adapter._write(self.adapter._path("commits", prepared["id"]), {
            "schema": responder.COMMIT_RECORD_SCHEMA,
            "schema_version": responder.SCHEMA_VERSION,
            "request_id": prepared["id"],
            "request_revision": prepared["request_revision"],
            "state": "prepared", "origin": "remote",
            "retain_until": self.now + 60,
        })
        conflict = self.adapter.expire(
            prepared["id"], prepared["request_revision"])
        self.assertEqual(conflict["event"]["type"], "conflict")
        self.assertEqual(conflict["event"]["detail"], "commit-prepared")
        self.assertTrue(os.path.exists(os.path.join(
            self.spool, "requests", prepared["id"] + ".json")))

    def test_already_answered_event_emits_once_then_fifo_advances(self):
        first = self.request("req-a", created=self.now, mode="text")
        later = self.request("req-z", created=self.now + 1, mode="text")
        self._write(os.path.join(self.spool, "answers", "req-a.json"),
                    {"text": "won elsewhere", "cancelled": False, "images": []})
        event = self.acquire()
        self.assertEqual(event["event"]["type"], "already-answered")
        explicit = self.acquire(resume={
            "request_id": first["id"], "revision": first["request_revision"],
            "token": "old-token"})
        self.assertEqual(explicit["event"]["type"], "already-answered")
        next_result = self.acquire()
        self.assertEqual(next_result["event"]["request"]["id"], later["id"])

    def test_metadata_ttl_prunes_but_preserves_recoverable_prepared_wal(self):
        request = self.request(mode="text", deadline=self.now + 2000)
        lease = self.acquire()["event"]["request"]["lease"]
        answer = {"schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                  "mode": "text", "text": "durable", "note": ""}

        def crash(boundary):
            if boundary == "after-prepare":
                raise RuntimeError("prepared crash")

        crashing = responder.Responder(
            self.spool, clock=lambda: self.now, fault_hook=crash)
        result = crashing.submit(
            request_id=request["id"], revision=request["request_revision"],
            lease_token=lease["token"], geometry_generation=7,
            submission_id="prepared-1", answer=answer)
        self.assertEqual(result["event"]["type"], "unavailable")
        commit_path = os.path.join(self.spool, "responder", "commits",
                                   request["id"] + ".json")
        self.now += responder.RECORD_RETENTION_SECONDS + 1
        self.adapter.local_available(request)
        self.assertTrue(os.path.exists(commit_path))

        recovered = self.adapter.submit(
            request_id=request["id"], revision=request["request_revision"],
            lease_token=lease["token"], geometry_generation=7,
            submission_id="prepared-1", answer=answer)
        self.assertEqual(recovered["event"]["type"], "committed")
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        with open(answer_path) as stream:
            persisted = json.load(stream)
        self.adapter.acknowledge_consumed(request, persisted)
        os.unlink(answer_path)
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        self.now += responder.RECORD_RETENTION_SECONDS + 1
        self.adapter.acquire("phone", 5)
        self.assertFalse(os.path.exists(commit_path))
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "responder", "terminal", request["id"] + ".json")))

    def test_bad_checklist_digest_bool_generation_and_signing_failure_fail_closed(self):
        bad = self.request("req-bad")
        bad["request_digest"] = "sha256:" + "0" * 64
        self._write(os.path.join(self.spool, "requests", "req-bad.json"), bad)
        self.assertEqual(self.acquire()["event"]["type"], "malformed")
        os.unlink(os.path.join(self.spool, "requests", "req-bad.json"))

        request = self.request("req-ok")
        leased = self.acquire()
        token = leased["event"]["request"]["lease"]["token"]
        args = dict(request_id=request["id"],
                    revision=request["request_revision"], lease_token=token,
                    geometry_generation=True, submission_id="bad-bool",
                    answer=self.checklist_answer(request))
        self.assertEqual(self.adapter.submit(**args)["event"]["type"],
                         "malformed")
        args["geometry_generation"] = 7
        args["submission_id"] = "signing-fails"
        with mock.patch.object(server, "build_checklist_receipt",
                               side_effect=RuntimeError("key offline")):
            failed = self.adapter.submit(**args)
        self.assertEqual(failed["event"]["type"], "unavailable")
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "answers", request["id"] + ".json")))
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "responder", "commits", request["id"] + ".json")))

    def test_system_target_error_terminates_remote_lease_with_typed_event(self):
        request = self.request(mode="text")
        leased = self.acquire()
        result = self.adapter.submit_local(
            request["id"], {"text": "", "cancelled": False, "images": [],
                            "error": {"kind": "target-unresolved",
                                      "reason": "ambiguous", "matches": 2}},
            submission_id="target-error", lifecycle=True)
        self.assertTrue(result["ok"])
        explicit = self.acquire(resume={
            "request_id": request["id"],
            "revision": request["request_revision"],
            "token": leased["event"]["request"]["lease"]["token"]})
        self.assertEqual(explicit["event"]["type"], "ambiguous")

    def test_local_text_only_checklist_preserves_prose_without_any_receipt(self):
        request = self.request()
        body = {"text": "operator used free prose", "cancelled": False,
                "images": [{"mime": "image/png", "b64": "legacy-pixels"}]}
        result = self.adapter.submit_local(request["id"], body)
        self.assertTrue(result["ok"])
        self.assertTrue(result["event"]["compatibility"])
        self.assertIsNone(result["event"]["receipt"])
        with open(os.path.join(
                self.spool, "answers", request["id"] + ".json")) as stream:
            self.assertEqual(json.load(stream), body)

    def test_legacy_local_answer_is_exact_no_clobber_and_not_remote_leasable(self):
        request = {"id": "legacy-a", "summary": "Old server request",
                   "created": self.now, "deadline": self.now + 60}
        self._write(os.path.join(self.spool, "requests", "legacy-a.json"), request)
        answer = {"text": "legacy", "cancelled": False,
                  "images": [{"mime": "image/png", "b64": "abc"}]}
        committed = self.adapter.submit_local("legacy-a", answer)
        self.assertTrue(committed["ok"])
        self.assertTrue(committed["event"]["legacy"])
        with open(os.path.join(self.spool, "answers", "legacy-a.json")) as stream:
            self.assertEqual(json.load(stream), answer)
        self.assertFalse(os.path.exists(os.path.join(
            self.spool, "responder", "commits", "legacy-a.json")))
        loser = self.adapter.submit_local("legacy-a", {**answer, "text": "other"})
        self.assertEqual(loser["event"]["type"], "already-answered")

        os.unlink(os.path.join(self.spool, "answers", "legacy-a.json"))
        remote = self.acquire()
        self.assertIn(remote["event"]["type"], ("malformed", "malformed-state"))

    def test_target_state_transitions_stale_validation_and_lease_expiry(self):
        request = self.request()
        os.unlink(os.path.join(self.spool, "targets", request["id"] + ".json"))
        missing = self.acquire()
        self.assertTrue(missing["ok"])
        self.assertEqual(missing["event"]["type"], "missing")
        token = missing["event"]["request"]["lease"]["token"]
        self.assertTrue(self.adapter.release(
            request["id"], request["request_revision"], token)["ok"])

        rows = [{"id": row["id"], "state": "untargeted",
                 "identity": None, "geometry": None}
                for row in request["responder"]["steps"]]
        rows[0] = {"id": rows[0]["id"], "state": "ambiguous",
                   "reason": "ambiguous", "matches": 2,
                   "identity": None, "geometry": None}
        target_path = os.path.join(self.spool, "targets", request["id"] + ".json")
        self._write(target_path, {
            "schema": responder.TARGET_SCHEMA, "schema_version": 1,
            "request_id": request["id"],
            "request_revision": request["request_revision"],
            "generation": 8, "state": "ambiguous", "targets": rows})
        ambiguous = self.acquire()
        self.assertEqual(ambiguous["event"]["type"], "ambiguous")
        token = ambiguous["event"]["request"]["lease"]["token"]
        moved = self.adapter.renew(
            request["id"], request["request_revision"], token, 10, 7)
        self.assertEqual(moved["event"]["type"], "ambiguous")

        # A resolved target whose aggregate generation changed is explicitly
        # moved until the caller acknowledges that exact generation.
        self.request("replacement")
        with open(os.path.join(self.spool, "targets", "replacement.json")) as stream:
            resolved_target = json.load(stream)
        resolved_target["request_id"] = request["id"]
        resolved_target["request_revision"] = request["request_revision"]
        resolved_target["generation"] = 9
        resolved_target["targets"] = [
            {**resolved_target["targets"][0], "id": rows[0]["id"]},
            {"id": rows[1]["id"], "state": "untargeted",
             "identity": None, "geometry": None},
        ]
        self._write(target_path, resolved_target)
        moved = self.adapter.renew(
            request["id"], request["request_revision"], token, 10, 8)
        self.assertTrue(moved["ok"])
        self.assertEqual(moved["event"]["type"], "moved")
        stale = self.adapter.submit(
            request_id=request["id"], revision="sha256:stale", lease_token=token,
            geometry_generation=9, submission_id="stale-submit",
            answer=self.checklist_answer(request))
        self.assertEqual(stale["event"]["type"], "stale")
        malformed = self.adapter.submit(
            request_id=request["id"], revision=request["request_revision"],
            lease_token=token, geometry_generation=9,
            submission_id="bad-order", answer={
                "schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                "mode": "checklist", "note": "",
                "steps": list(reversed(self.checklist_answer(request)["steps"]))})
        self.assertEqual(malformed["event"]["type"], "malformed")
        self.now += 11
        expired_lease = self.adapter.renew(
            request["id"], request["request_revision"], token, 10, 9)
        self.assertEqual(expired_lease["event"]["type"], "lease-expired")

    def test_cancelled_terminal_and_strict_jsonl_command_surface(self):
        request = self.request(mode="text")
        self.adapter.record_terminal(request, "cancelled", "caller cancelled")
        result = self.acquire(resume={
            "request_id": request["id"],
            "revision": request["request_revision"], "token": "old"})
        self.assertEqual(result["event"]["type"], "cancelled")

        command = {
            "schema": responder.COMMAND_SCHEMA, "schema_version": 1,
            "op": "acquire", "consumer": "phone", "lease_seconds": 5,
            "unexpected": True,
        }
        def one_shot(value):
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "responder.py"),
                 "--one-shot", "--spool", self.spool],
                input=json.dumps(value) + "\n", text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            lines = proc.stdout.splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(proc.stderr, "")
            return json.loads(lines[0])

        self.assertEqual(one_shot(command)["event"]["type"], "malformed")
        common = {"schema": responder.COMMAND_SCHEMA, "schema_version": 1}
        malformed_commands = [
            [],
            {**common, "op": "acquire", "consumer": "phone",
             "lease_seconds": 5, "resume": "junk"},
            {**common, "op": "acquire", "consumer": "phone",
             "lease_seconds": 5,
             "resume": {"request_id": request["id"],
                        "revision": request["request_revision"]}},
            {**common, "op": "acquire", "consumer": "phone",
             "lease_seconds": 5,
             "resume": {"request_id": request["id"],
                        "revision": request["request_revision"],
                        "token": "old", "unexpected": True}},
            {**common, "op": "renew", "revision": request["request_revision"],
             "lease_token": "old", "lease_seconds": 5,
             "acknowledged_geometry_generation": 7},
            {**common, "op": "release", "request_id": request["id"],
             "revision": request["request_revision"]},
        ]
        for malformed_command in malformed_commands:
            with self.subTest(command=malformed_command):
                self.assertEqual(one_shot(malformed_command)["event"]["type"],
                                 "malformed")

        oversized = "{" + " " * (responder.MAX_COMMAND_BYTES + 1) + "}\n"
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "responder.py"),
             "--one-shot", "--spool", self.spool], input=oversized,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        self.assertEqual(json.loads(proc.stdout)["event"]["type"], "malformed")

    def orphan_prepare(self, *, boundary="after-prepare", mode="text", verdict="pass", deadline=None, answer_override=None):
        request = self.request(mode=mode, deadline=deadline)
        acquired = self.acquire()["event"]["request"]
        answer = (self.checklist_answer(request, verdict) if mode == "checklist" else {
            "schema": responder.ANSWER_SCHEMA, "schema_version": 1,
            "mode": "text", "text": "Exact original typed response", "note": ""})
        if answer_override is not None:
            answer = answer_override
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"], geometry_generation=7,
                    submission_id="orphaned-original", answer=answer)
        now = self.now
        def child():
            def crash(at):
                if at == boundary:
                    os._exit(73)
            result = responder.Responder(self.spool, clock=lambda: now,
                                         fault_hook=crash).submit(**args)
            os._exit(0 if result["ok"] else 1)
        process = multiprocessing.get_context("fork").Process(target=child)
        process.start(); process.join(timeout=3)
        if process.is_alive():
            process.terminate(); process.join()
            self.fail("temporary transaction child did not exit")
        self.assertEqual(process.exitcode, 0 if boundary == "after-receipt" else 73)
        return request, acquired, args

    def commit_record(self, request):
        path = os.path.join(self.spool, "responder", "commits", request["id"] + ".json")
        with open(path) as stream:
            return path, json.load(stream)

    def test_orphan_process_loss_recovers_original_authenticated_answer(self):
        request, acquired, args = self.orphan_prepare()
        path, record = self.commit_record(request)
        self.assertEqual(record["recovery_answer"], args["answer"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn("lease_token", record)
        self.assertNotIn(acquired["lease"]["token"], json.dumps(record))
        self.assertEqual(self.adapter.recover(request["id"], acquired["revision"])["event"]["type"], "conflict")
        self.now += 11
        fresh = responder.Responder(self.spool, clock=lambda: self.now)
        result = fresh.dispatch({"schema": responder.COMMAND_SCHEMA, "schema_version": 1,
                                 "op": "recover", "request_id": request["id"],
                                 "revision": acquired["revision"]})
        self.assertTrue(result["ok"])
        self.assertTrue(responder.verify_remote_receipt(result["event"]["receipt"],
            commit_receipt=result["event"]["commit_receipt"], request=acquired,
            **{k: v for k, v in args.items() if k not in ("request_id", "revision")})["valid"])
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            self.assertEqual(json.load(stream)["text"], args["answer"]["text"])

    def test_orphan_after_publish_never_replaces_visible_answer(self):
        request, acquired, args = self.orphan_prepare(boundary="after-publish")
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        with open(answer_path, "rb") as stream:
            before = stream.read()
        inode = os.stat(answer_path).st_ino
        self.now += 11
        self.assertTrue(self.adapter.recover(request["id"], acquired["revision"])["ok"])
        with open(answer_path, "rb") as stream:
            self.assertEqual(stream.read(), before)
        self.assertEqual(os.stat(answer_path).st_ino, inode)
        self.assertEqual(self.adapter.submit(**args)["event"]["type"], "idempotent")
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])

    def test_orphan_after_receipt_is_already_answered(self):
        request, acquired, args = self.orphan_prepare(boundary="after-receipt")
        result = self.adapter.recover(request["id"], acquired["revision"])
        self.assertEqual(result["event"]["type"], "already-answered")
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])
        self.assertEqual(self.adapter.submit(**args)["event"]["type"], "idempotent")

    def test_new_acquire_recovers_expired_orphan_before_next_fifo_head(self):
        request, _, _ = self.orphan_prepare()
        self.request("req-z", created=self.now + 1, mode="text")
        self.assertEqual(self.acquire()["event"]["type"], "conflict")
        self.now += 11
        fresh = responder.Responder(self.spool, clock=lambda: self.now)
        self.assertEqual(fresh.acquire("new-consumer", 5)["event"]["type"], "already-answered")
        next_head = fresh.acquire("new-consumer", 5)
        self.assertEqual(next_head["event"]["request"]["id"], "req-z")
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])

    def test_orphan_expiry_finishes_prior_negative_without_authorization_upgrade(self):
        request, acquired, args = self.orphan_prepare(mode="checklist", verdict="not_done", deadline=self.now + 3)
        original_receipt = self.commit_record(request)[1]["receipt"]
        self.now += responder.RECORD_RETENTION_SECONDS + 1
        result = self.adapter.recover(request["id"], acquired["revision"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["event"]["receipt"], original_receipt)
        self.assertFalse(result["event"]["receipt"]["authorizing"])
        self.assertEqual(self.adapter.submit(**args)["event"]["type"], "idempotent")

    def test_orphan_cancel_cleanup_removes_payload_and_preserves_terminal(self):
        request, acquired, _ = self.orphan_prepare()
        self.adapter.record_terminal(request, "cancelled")
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])
        result = self.adapter.recover(request["id"], acquired["revision"])
        self.assertEqual(result["event"]["type"], "cancelled")
        self.assertNotIn("receipt", result["event"])
        self.assertFalse(os.path.exists(os.path.join(self.spool, "answers", request["id"] + ".json")))

    def test_orphan_acknowledged_before_cleanup_cannot_resurrect_answer(self):
        request, acquired, _ = self.orphan_prepare(boundary="after-publish", mode="checklist")
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        with open(answer_path) as stream:
            answer = json.load(stream)
        self.assertTrue(self.adapter.acknowledge_consumed(request, answer)["ok"])
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])
        os.unlink(answer_path)
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        self.assertEqual(self.adapter.recover(request["id"], acquired["revision"])["event"]["type"], "already-answered")
        self.assertFalse(os.path.exists(answer_path))

    def test_orphan_payload_receipt_and_capture_tamper_fail_closed(self):
        request, acquired, _ = self.orphan_prepare(mode="checklist")
        self.now += 11
        path, pristine = self.commit_record(request)
        cases = []
        changed = copy.deepcopy(pristine); changed["recovery_answer"]["note"] = "substituted note"; cases.append(changed)
        changed = copy.deepcopy(pristine); changed["recovery_answer"]["image"] = "forbidden"; cases.append(changed)
        changed = copy.deepcopy(pristine); changed["commit_receipt"]["submission"]["id"] = "wrong"; cases.append(changed)
        changed = copy.deepcopy(pristine); changed["receipt"]["authorizing"] = False; cases.append(changed)
        for changed in cases:
            self._write(path, changed)
            self.assertFalse(self.adapter.recover(request["id"], acquired["revision"])["ok"])
            self.assertFalse(os.path.exists(os.path.join(self.spool, "answers", request["id"] + ".json")))
        self._write(path, pristine)
        self.assertTrue(self.adapter.recover(request["id"], acquired["revision"])["ok"])

    def test_orphan_recovery_races_local_writer_and_second_recovery(self):
        request, acquired, _ = self.orphan_prepare()
        self.now += 11
        barrier = threading.Barrier(3)
        results = []
        def recover():
            barrier.wait()
            results.append(self.adapter.recover(request["id"], acquired["revision"])["event"]["type"])
        def local():
            barrier.wait()
            results.append(self.adapter.submit_local(request["id"], {"text": "different local answer", "cancelled": False, "images": []})["event"]["type"])
        threads = [threading.Thread(target=recover), threading.Thread(target=recover), threading.Thread(target=local)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results.count("committed"), 1)
        self.assertTrue(all(kind in ("committed", "conflict", "already-answered") for kind in results))
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            self.assertEqual(json.load(stream)["text"], "Exact original typed response")

    def test_legacy_metadata_orphan_requires_original_exact_retry(self):
        request, acquired, args = self.orphan_prepare()
        path, record = self.commit_record(request)
        record.pop("recovery_answer")
        self._write(path, record)
        self.now += 11
        self.assertEqual(self.adapter.recover(request["id"], acquired["revision"])["event"]["type"], "recovery-unavailable")
        self.assertEqual(self.acquire()["event"]["type"], "conflict")
        self.assertTrue(self.adapter.submit(**args)["ok"])

    def test_orphan_cleanup_of_missing_request_discards_extra_answer(self):
        request, acquired, _ = self.orphan_prepare()
        os.unlink(os.path.join(self.spool, "requests", request["id"] + ".json"))
        self.assertFalse(self.adapter.recover(request["id"], acquired["revision"])["ok"])
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])

    def test_orphan_recovery_never_clobbers_mismatched_existing_answer(self):
        request, acquired, _ = self.orphan_prepare()
        self.now += 11
        answer_path = os.path.join(self.spool, "answers", request["id"] + ".json")
        self._write(answer_path, {"text": "already visible unrelated bytes"})
        with open(answer_path, "rb") as stream:
            before = stream.read()
        self.assertEqual(self.adapter.recover(request["id"], acquired["revision"])["event"]["type"], "conflict")
        with open(answer_path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_orphan_recovery_api_closed_and_revision_bound(self):
        request, acquired, _ = self.orphan_prepare()
        command = {"schema": responder.COMMAND_SCHEMA, "schema_version": 1,
                   "op": "recover", "request_id": request["id"], "revision": acquired["revision"]}
        self.assertEqual(self.adapter.dispatch({**command, "answer": {}})["event"]["type"], "malformed")
        self.assertEqual(self.adapter.dispatch({**command, "revision": "sha256:" + "0" * 64})["event"]["type"], "stale")

    def test_remote_recovery_payload_has_independent_byte_bound(self):
        request = self.request(mode="text")
        acquired = self.acquire()["event"]["request"]
        answer = {"schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                  "mode": "text", "text": "🙂" * responder.MAX_TEXT_CHARS, "note": ""}
        result = self.adapter.submit(request_id=request["id"], revision=acquired["revision"],
            lease_token=acquired["lease"]["token"], geometry_generation=7, submission_id="oversized", answer=answer)
        self.assertEqual(result["event"]["type"], "malformed")
        self.assertFalse(os.path.exists(os.path.join(self.spool, "responder", "commits", request["id"] + ".json")))

    def test_orphan_terminal_cleanup_interruption_is_reconciled(self):
        request, acquired, _ = self.orphan_prepare()
        original = self.adapter._write
        def fault(path, value):
            if "/commits/" in path and "recovery_answer" not in value:
                raise OSError("synthetic cleanup interruption")
            original(path, value)
        self.adapter._write = fault
        self.assertFalse(self.adapter.record_terminal(request, "cancelled")["ok"])
        self.assertIn("recovery_answer", self.commit_record(request)[1])
        fresh = responder.Responder(self.spool, clock=lambda: self.now)
        self.assertEqual(fresh.recover(request["id"], acquired["revision"])["event"]["type"], "cancelled")
        self.assertNotIn("recovery_answer", self.commit_record(request)[1])

    @staticmethod
    def completion_answer(mode, *, incomplete=False, marker=True):
        answer = {"schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                  "mode": mode, "note": "Reason for this response"}
        if marker:
            answer["completion"] = "not_done" if incomplete else "complete"
        if mode == "choice":
            answer["choices"] = [] if incomplete else ["direct"]
        elif mode in ("text", "prose"):
            answer["text"] = " \n\t" if incomplete else "Typed response"
        else:
            answer["confirmed"] = not incomplete
        return answer

    def test_explicit_incomplete_modes_commit_and_report_not_done(self):
        for index, mode in enumerate(("choice", "text", "prose", "ack")):
            with self.subTest(mode=mode):
                request = self.request("incomplete-" + mode, mode=mode, created=self.now + index)
                for _ in range(4):
                    acquired_result = self.acquire()
                    if acquired_result["ok"]: break
                acquired = acquired_result["event"]["request"]
                answer = self.completion_answer(mode, incomplete=True)
                args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"], geometry_generation=7,
                    submission_id="incomplete-submission", answer=answer)
                result = self.adapter.submit(**args)
                self.assertTrue(result["ok"])
                event = result["event"]
                self.assertEqual(event["receipt"]["completion"], "not_done")
                self.assertFalse(event["receipt"]["authorizing"])
                checked = responder.verify_remote_receipt(event["receipt"], commit_receipt=event["commit_receipt"],
                    request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
                self.assertTrue(checked["valid"])
                self.assertFalse(checked["authorizing"])
                with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
                    stored = json.load(stream)
                self.assertNotIn("choice", stored)
                self.assertNotIn("confirmed", stored)
                self.assertFalse(stored["cancelled"])
                self.assertEqual(stored["response_completion"]["status"], "not_done")
                content = server.answer_content(stored)
                self.assertIn("NOT DONE", content[0]["text"])
                texts = [row["text"] for row in content]
                self.assertEqual(texts[texts.index("[Global note]") + 1], "Reason for this response")
                self.assertNotIn("(operator sent no text)", json.dumps(content))
                self.assertNotIn("[selected:", json.dumps(content))
                replay = self.adapter.submit(**args)
                self.assertEqual(replay["event"]["type"], "idempotent")
                self.assertEqual(replay["event"]["receipt"], event["receipt"])

    def test_complete_markers_optional_and_legacy_local_ack_unchanged(self):
        for mode in ("choice", "text", "prose", "ack"):
            request = self.request(mode=mode)
            for marker in (False, True):
                internal = responder.Responder._validate_remote_answer(request, self.completion_answer(mode, marker=marker))
                self.assertNotIn("response_completion", internal)
        self.assertEqual(server.answer_content({"text": "", "cancelled": False, "images": []}),
                         [{"type": "text", "text": "(operator sent no text)"}])

    def test_incomplete_markers_absent_invalid_or_contradictory_are_refused(self):
        for mode in ("choice", "text", "prose", "ack"):
            request = self.request(mode=mode)
            incomplete = self.completion_answer(mode, incomplete=True)
            cases = []
            missing = copy.deepcopy(incomplete); missing.pop("completion"); cases.append(missing)
            for value in (None, False, True, "", "done", "complete", [], {}):
                changed = copy.deepcopy(incomplete); changed["completion"] = value; cases.append(changed)
            filled = self.completion_answer(mode); filled["completion"] = "not_done"; cases.append(filled)
            unknown = copy.deepcopy(incomplete); unknown["extra"] = True; cases.append(unknown)
            for answer in cases:
                with self.subTest(mode=mode, case=len(answer)):
                    with self.assertRaises(responder.AdapterError):
                        responder.Responder._validate_remote_answer(request, answer)
        request = self.request()
        answer = self.checklist_answer(request, "not_done")
        answer["completion"] = "not_done"
        with self.assertRaises(responder.AdapterError):
            responder.Responder._validate_remote_answer(request, answer)

    def test_incomplete_receipt_requires_signed_exact_completion(self):
        request = self.request(mode="ack")
        acquired = self.acquire()["event"]["request"]
        answer = self.completion_answer("ack", incomplete=True)
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"], geometry_generation=7,
                    submission_id="incomplete-ack", answer=answer)
        result = self.adapter.submit(**args)["event"]
        def verify(receipt, expected_answer=answer):
            return responder.verify_remote_receipt(receipt, commit_receipt=receipt,
                request=acquired, geometry_generation=7, submission_id=args["submission_id"],
                answer=expected_answer, lease_token=args["lease_token"])
        altered = copy.deepcopy(result["receipt"]); altered["completion"] = "complete"
        self.assertFalse(verify(altered)["valid"])
        # Even a validly signed receipt with the same exact answer digest must
        # not omit or contradict the independently expected completion state.
        wrong = self.adapter._commit_receipt(request, args["submission_id"],
            responder.submission_digest(request["id"], acquired["revision"], 7, args["submission_id"], answer,
                                        lease_token=args["lease_token"]), responder.answer_digest(answer), self.now)
        self.assertFalse(verify(wrong)["valid"])
        complete = self.completion_answer("ack")
        self.assertFalse(verify(result["receipt"], complete)["valid"])

    def test_incomplete_answer_survives_complete_controller_loss(self):
        answer = self.completion_answer("text", incomplete=True)
        request, acquired, args = self.orphan_prepare(answer_override=answer)
        original_receipt = self.commit_record(request)[1]["receipt"]
        self.now += 11
        recovered = self.adapter.recover(request["id"], acquired["revision"])
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["event"]["receipt"], original_receipt)
        self.assertFalse(recovered["event"]["receipt"]["authorizing"])
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            stored = json.load(stream)
        self.assertIn("NOT DONE", server.answer_content(stored)[0]["text"])
        checked = responder.verify_remote_receipt(recovered["event"]["receipt"],
            commit_receipt=recovered["event"]["commit_receipt"], request=acquired,
            **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertTrue(checked["valid"])

    def exact_submit(self, *, mode="checklist", incomplete=False):
        request = self.request(mode=mode)
        acquired = self.acquire()["event"]["request"]
        note = "  Global α note\nsecond line\t  "
        if mode == "checklist":
            answer = self.checklist_answer(request, "not_done" if incomplete else "pass")
            answer["steps"][0]["note"] = " \t First 雪 note\n second line  "
            answer["steps"][1]["note"] = "\t \n"
        else:
            answer = self.completion_answer(mode, incomplete=incomplete)
            if mode in ("text", "prose"):
                answer["text"] = " \t\n" if incomplete else "  Exact text 雪\nnext line\t "
        answer["note"] = note
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"], geometry_generation=7,
                    submission_id="exact-fields", answer=answer)
        result = self.adapter.submit(**args)
        self.assertTrue(result["ok"])
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            stored = json.load(stream)
        return request, acquired, args, result["event"], stored

    def test_exact_checklist_notes_survive_storage_receipt_and_mcp_blocks(self):
        request, acquired, args, event, stored = self.exact_submit()
        self.assertEqual(stored["remote_response"]["answer"], args["answer"])
        self.assertEqual(event["commit_receipt"]["answer_format"], responder.REMOTE_EXACT_FORMAT)
        for sent, canonical, receipt in zip(args["answer"]["steps"], stored["checklist"]["steps"], event["receipt"]["steps"]):
            self.assertEqual(sent["note"], canonical["note"])
            self.assertEqual(sent["note"], receipt["note"])
        content = [row["text"] for row in server.answer_content(stored, request=request)]
        for label, value in (("[Step 1 note]", args["answer"]["steps"][0]["note"]),
                             ("[Step 2 note]", args["answer"]["steps"][1]["note"]),
                             ("[Global note]", args["answer"]["note"])):
            self.assertEqual(content[content.index(label) + 1], value)
        verified = responder.verify_remote_receipt(event["receipt"], commit_receipt=event["commit_receipt"],
            request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertTrue(verified["valid"])
        self.assertTrue(verified["exact_text"])

    def test_exact_text_and_global_note_remain_distinct_in_mcp(self):
        request, acquired, args, event, stored = self.exact_submit(mode="text")
        content = [row["text"] for row in server.answer_content(stored, request=request)]
        self.assertEqual(content, ["[Text answer]", args["answer"]["text"],
                                   "[Global note]", args["answer"]["note"]])
        self.assertEqual(stored["remote_response"]["answer"], args["answer"])
        self.assertEqual(self.adapter.submit(**args)["event"]["receipt"], event["receipt"])

    def test_exact_incomplete_text_retains_whitespace_without_acknowledging(self):
        request, _, args, event, stored = self.exact_submit(mode="text", incomplete=True)
        content = [row["text"] for row in server.answer_content(stored, request=request)]
        self.assertIn("NOT DONE", content[0])
        self.assertEqual(content[content.index("[Text answer]") + 1], args["answer"]["text"])
        self.assertEqual(content[content.index("[Global note]") + 1], args["answer"]["note"])
        self.assertFalse(event["receipt"]["authorizing"])

    def test_exact_whitespace_recovery_preserves_original_signed_values(self):
        answer = self.completion_answer("text")
        answer.update(text="  原文\n  ", note=" \tNote\n ")
        request, acquired, args = self.orphan_prepare(answer_override=answer)
        self.now += 11
        result = self.adapter.recover(request["id"], acquired["revision"])
        self.assertTrue(result["ok"])
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            stored = json.load(stream)
        self.assertEqual(stored["remote_response"]["answer"], answer)
        self.assertEqual(server.verified_remote_response(stored, request), answer)
        verified = responder.verify_remote_receipt(result["event"]["receipt"], commit_receipt=result["event"]["commit_receipt"],
            request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertTrue(verified["exact_text"])

    def legacy_prepare(self):
        request = self.request()
        acquired = self.acquire()["event"]["request"]
        answer = self.checklist_answer(request)
        answer["note"] = "  historical global  "
        answer["steps"][0]["note"] = "  historical step  "
        args = dict(request_id=request["id"], revision=acquired["revision"],
                    lease_token=acquired["lease"]["token"], geometry_generation=7,
                    submission_id="historical-mapping", answer=answer)
        internal = responder.Responder._validate_remote_answer(request, answer, exact=False)
        def crash(boundary):
            if boundary == "after-prepare":
                raise RuntimeError("synthetic historical interruption")
        adapter = responder.Responder(self.spool, clock=lambda: self.now, fault_hook=crash)
        with self.assertRaises(RuntimeError):
            with adapter._transaction():
                adapter._commit_locked(request, internal, args["submission_id"],
                    responder.submission_digest(request["id"], acquired["revision"], 7, args["submission_id"],
                                                answer, lease_token=args["lease_token"]),
                    responder.answer_digest(answer), remote_answer=answer)
        return request, acquired, args

    def test_legacy_prepared_recovery_does_not_upgrade_signed_whitespace(self):
        request, acquired, args = self.legacy_prepare()
        original_receipt = self.commit_record(request)[1]["receipt"]
        self.now += 11
        result = self.adapter.recover(request["id"], acquired["revision"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["event"]["receipt"], original_receipt)
        self.assertEqual(original_receipt["steps"][0]["note"], "historical step")
        verified = responder.verify_remote_receipt(result["event"]["receipt"], commit_receipt=result["event"]["commit_receipt"],
            request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertTrue(verified["valid"])
        self.assertFalse(verified["exact_text"])
        with open(os.path.join(self.spool, "answers", request["id"] + ".json")) as stream:
            stored = json.load(stream)
        self.assertNotIn("remote_response", stored)
        self.assertEqual(self.adapter.submit(**args)["event"]["receipt"], original_receipt)

    def test_legacy_exact_submit_retry_uses_original_mapping(self):
        request, _, args = self.legacy_prepare()
        original_receipt = self.commit_record(request)[1]["receipt"]
        result = self.adapter.submit(**args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["event"]["receipt"], original_receipt)

    def test_exact_format_and_remote_text_tamper_fail_closed(self):
        request, acquired, args, event, stored = self.exact_submit()
        changed = copy.deepcopy(stored)
        changed["remote_response"]["answer"]["note"] = args["answer"]["note"].strip()
        self.assertIsNone(server.verified_remote_response(changed, request))
        self.assertIn("NOT DONE", server.answer_content(changed, request=request)[0]["text"])
        changed = copy.deepcopy(stored); changed.pop("commit_receipt")
        self.assertIsNone(server.verified_remote_response(changed, request))
        changed = copy.deepcopy(event["commit_receipt"]); changed.pop("answer_format")
        verified = responder.verify_remote_receipt(event["receipt"], commit_receipt=changed,
            request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertFalse(verified["valid"])
        # A separately authentic normalized checklist is not exact-note proof.
        normalized = server.build_checklist_receipt(request,
            responder.Responder._validate_remote_answer(request, args["answer"], exact=False), answered_at=self.now)
        verified = responder.verify_remote_receipt(normalized, commit_receipt=event["commit_receipt"],
            request=acquired, **{k: v for k, v in args.items() if k not in ("request_id", "revision")})
        self.assertFalse(verified["valid"])

    def test_local_checklist_receipt_and_content_keep_legacy_normalization(self):
        request = self.request()
        internal = responder.Responder._validate_remote_answer(request, self.checklist_answer(request), exact=False)
        internal["checklist"]["steps"][0]["note"] = "  local note  "
        receipt = server.build_checklist_receipt(request, internal, answered_at=self.now)
        self.assertEqual(receipt["steps"][0]["note"], "local note")
        self.assertEqual(server.answer_content({"text": "  local text  ", "images": [], "cancelled": False}),
                         [{"type": "text", "text": "local text"}])

    def run_temporary_mcp_answer(self, *, tamper=False, mode="text", local=None):
        note, text = "  global 雪\n ", "  body α\n \t"
        def publish(spool, request):
            if local is not None:
                result = responder.Responder(spool).submit_local(request["id"], local)
                self.assertTrue(result["ok"], result)
                return
            target = {"schema": responder.TARGET_SCHEMA, "schema_version": 1,
                      "request_id": request["id"], "request_revision": request["request_revision"],
                      "generation": 7, "state": "untargeted", "observed_at": self.now,
                      "targets": [{"id": row["id"], "state": "untargeted", "identity": None, "geometry": None}
                                  for row in request["responder"]["steps"]]}
            self._write(os.path.join(spool, "targets", request["id"] + ".json"), target)
            adapter = responder.Responder(spool)
            acquired = adapter.acquire("temporary-remote", 10)["event"]["request"]
            answer = {"schema": responder.ANSWER_SCHEMA, "schema_version": 1,
                      "mode": mode, "note": note}
            if mode == "checklist":
                answer["steps"] = [{"id": row["id"], "verdict": "pass", "note": note}
                                   for row in request["responder"]["steps"]]
            else:
                answer["text"] = text
            result = adapter.submit(request_id=request["id"], revision=acquired["revision"],
                lease_token=acquired["lease"]["token"], geometry_generation=7, submission_id="temporary-mcp", answer=answer)
            self.assertTrue(result["ok"])
            if tamper:
                path = os.path.join(spool, "answers", request["id"] + ".json")
                with open(path) as stream: stored = json.load(stream)
                if tamper == "strip-all":
                    for field in ("remote_response", "commit_receipt", "receipt", "responder_commit"):
                        stored.pop(field, None)
                elif tamper == "strip-display":
                    stored.pop("remote_response")
                    stored.pop("commit_receipt")
                else:
                    stored["remote_response"]["answer"]["note"] = "forged"
                self._write(path, stored)
        patches = {
            "capability_guard": lambda: contextlib.nullcontext(True), "capability_enabled": lambda: True,
            "instance_stamp": lambda: {"session": None, "pane": None, "server_uuid": "b" * 32, "attached": False},
            "unattached_refusal": lambda inst: None, "spool_dir": lambda: self.spool,
            "reclaim_answered_orphans": lambda spool: None, "hold_palette": lambda inst, spool: None,
            "caller_process_identity": lambda: None, "arrival_nudge": publish,
            "tui_alive": lambda spool, iid: True, "clear_arrival_nudge": lambda spool, rid: None,
        }
        with mock.patch.multiple(server, **patches):
            summary = "Temporary exact response request."
            if mode == "checklist":
                summary += "\n1. Inspect the result"
            result = server.run_feedback(summary, 2, "temporary-rpc",
                                         {"mode": mode, "options": [
                                             {"value": "fixed", "label": "Fixed"},
                                             {"value": "pause", "label": "Pause"}
                                         ] if mode == "choice" else []})
        return result, text, note

    def test_mcp_wait_path_delivers_exact_distinct_remote_strings(self):
        result, text, note = self.run_temporary_mcp_answer()
        self.assertIsInstance(result, server.FeedbackResult)
        self.assertEqual(list(result), [{"type": "text", "text": "[Text answer]"},
                                       {"type": "text", "text": text},
                                       {"type": "text", "text": "[Global note]"},
                                       {"type": "text", "text": note}])
        self.assertEqual(result.structured["text"], text)
        self.assertEqual(result.structured["note"], note)
        self.assertEqual(result.structured["schema"], "human-copilot.response/v1")
        self.assertIsNone(result.structured["choice"])

    def test_mcp_typed_choice_never_infers_selection_from_prose(self):
        note = '[selected: "Fixed" (value: "fixed")] I cannot do this'
        incomplete = {"schema": "human-copilot.response-completion/v1", "schema_version": 1,
                      "mode": "choice", "status": "not_done"}
        result, _, _ = self.run_temporary_mcp_answer(mode="choice", local={
            "text": note, "cancelled": False, "images": [], "response_completion": incomplete})
        self.assertEqual(result.structured["completion"], "not_done")
        self.assertIsNone(result.structured["choice"])
        self.assertEqual(result.structured["note"], note)
        result, _, _ = self.run_temporary_mcp_answer(mode="choice", local={
            "text": "checked", "cancelled": False, "images": [],
            "choice": {"value": "fixed", "label": "Forged display label"}})
        self.assertEqual(result.structured["choice"], {"value": "fixed", "label": "Fixed"})
        self.assertEqual(result.structured["completion"], "complete")

    def test_mcp_typed_ack_and_cancelled_choice(self):
        result, _, _ = self.run_temporary_mcp_answer(mode="ack", local={
            "text": "", "cancelled": False, "images": []})
        self.assertEqual(result.structured["completion"], "complete")
        self.assertEqual(result.structured["mode"], "ack")
        result, _, _ = self.run_temporary_mcp_answer(mode="choice", local={
            "text": "", "cancelled": True, "images": []})
        self.assertEqual(result.structured["completion"], "cancelled")
        self.assertIsNone(result.structured["choice"])

    def test_typed_response_rejects_malformed_choice_and_remote_wrapper(self):
        request = self.request(mode="choice")
        for answer in ({"text": '[selected: "Direct" (value: "direct")]'},
                       {"text": "x", "choice": {"value": "unknown"}},
                       {"text": "x", "cancelled": "yes"},
                       {"text": "x", "remote_response": {}},
                       {"text": "x", "response_completion": {"status": "not_done"}}):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                server.structured_response(answer, request)

    def test_mcp_wait_path_refuses_storage_digest_conflict(self):
        result, _, _ = self.run_temporary_mcp_answer(tamper=True)
        self.assertIsInstance(result, server.FeedbackError)
        self.assertIsNone(result.structured)
        self.assertIn("NOT DONE", result.content[0]["text"])

    def test_mcp_remote_marker_downgrade_cannot_synthesize_authorization(self):
        for mode in ("text", "checklist"):
            for tamper in ("strip-display", "strip-all"):
                with self.subTest(mode=mode, tamper=tamper):
                    result, _, _ = self.run_temporary_mcp_answer(mode=mode, tamper=tamper)
                    self.assertIsInstance(result, server.FeedbackError)
                    self.assertIsNone(result.structured)
                    self.assertIn("NOT DONE", result.content[0]["text"])
                    # Failed acknowledgement deliberately retains the answer;
                    # isolate each independent corruption experiment's FIFO.
                    for group in ("requests", "answers"):
                        for name in os.listdir(os.path.join(self.spool, group)):
                            os.unlink(os.path.join(self.spool, group, name))

    def test_mcp_unavailable_acknowledgement_never_authorizes(self):
        unavailable = {"ok": False, "event": {"type": "unavailable"}}
        with mock.patch.object(responder.Responder, "acknowledge_consumed", return_value=unavailable):
            result, _, _ = self.run_temporary_mcp_answer(mode="checklist")
        self.assertIsInstance(result, server.FeedbackError)
        self.assertIsNone(result.structured)
        requests = os.listdir(os.path.join(self.spool, "requests"))
        self.assertEqual(len(requests), 1)
        rid = requests[0][:-5]
        with open(os.path.join(self.spool, "answers", rid + ".json")) as stream:
            answer = json.load(stream)
        self.assertEqual(answer["remote_response"]["answer"]["note"], "  global 雪\n ")
        self.assertFalse(os.path.exists(self.adapter._path("terminal", rid)))
        with open(os.path.join(self.spool, "requests", requests[0])) as stream:
            request = json.load(stream)
        self.assertTrue(self.adapter.acknowledge_consumed(request, answer)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
