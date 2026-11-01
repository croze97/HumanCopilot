"""Non-disruptive release-pipeline tests; every LIVE path is a temp fixture."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release

REPO = Path(__file__).resolve().parents[1]


class ReleasePipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hcop-release-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.live = self.root / "live"
        self.source.mkdir()
        inputs = list(release.CORE_FILES + release.TUI_FILES + release.VALIDATION_FILES)
        inputs.extend(
            f"overlay/{release.LIVE_UUID}/{item}" for item in release.OVERLAY_FILES
        )
        for rel in inputs:
            destination = self.source / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, destination)
        self._git("init", "-q")
        self._git("config", "user.name", "Release Test")
        self._git("config", "user.email", "release-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "-qm", "release A")

        (self.home / "bin").mkdir(parents=True)
        self.codex_home.mkdir(parents=True)
        extension_parent = self.home / ".local/share/gnome-shell/extensions"
        extension_parent.mkdir(parents=True)
        os.symlink(self.source / "tui.py", self.home / "bin/copilot-tui")
        os.symlink(self.source / "toggle.py", self.home / "bin/copilot-toggle")
        os.symlink(self.source / "open_window.py", self.home / "bin/copilot-open")
        os.symlink(
            self.source / f"overlay/{release.LIVE_UUID}",
            extension_parent / release.LIVE_UUID,
        )
        (self.home / ".claude.json").write_text(
            json.dumps(
                {
                    "unrelated": {"preserve": True},
                    "mcpServers": {
                        "human-copilot": {
                            "type": "stdio",
                            "command": str(self.source / "server.py"),
                            "timeout": 3600000,
                        },
                        "other": {"command": "/other/server"},
                    },
                },
                indent=2,
            )
            + "\n"
        )
        (self.codex_home / "config.toml").write_text(
            'model = "test"\n\n[mcp_servers.human-copilot]\n'
            f"command = {json.dumps(str(self.source / 'server.py'))}\n"
            'tool_timeout_sec = 3600\n\n[mcp_servers.other]\ncommand = "/other/server"\n'
        )

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.source, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _build(self):
        return release.build_release(self.source, self.state)

    def _automated(self, qa_id, artifact=None):
        artifact = artifact or Path(self._build()["artifact_dir"])
        deployment = release.deploy_qa(self.state, artifact_dir=artifact, qa_id=qa_id)
        validation = release.validate_qa(self.state, qa_id=qa_id)
        self.assertEqual(validation["automated"]["state"], "passed", validation)
        self.assertEqual(validation["state"], "pending-visual")
        return deployment, validation

    def _attest(self, qa_id, digest):
        return release.attest_visual(
            self.state,
            qa_root=None,
            qa_id=qa_id,
            release_digest=digest,
            result="passed",
            actor="unit-test-fixture",
            note="Synthetic attestation exercises the digest gate; it is not operator evidence.",
        )

    def _promote(self, qa_id=None, digest=None):
        return release.promote_live(
            self.state,
            qa_id=qa_id,
            release_digest=digest,
            confirm="attested",
            home=self.home,
            codex_home=self.codex_home,
            live_root=self.live,
            legacy_source_root=self.source,
            reload_overlay="never",
        )

    def _status_cli(self, component):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = release.main(
                [
                    "--state-root",
                    str(self.state),
                    "status",
                    component,
                    "--live-root",
                    str(self.live),
                    "--json",
                ]
            )
        self.assertEqual(result, 0, output.getvalue())
        return json.loads(output.getvalue())

    @staticmethod
    def _snapshot(root):
        result = {}
        if not root.exists():
            return result
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[rel] = ("link", os.readlink(path))
            elif path.is_file():
                result[rel] = (
                    "file",
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mode & 0o777,
                )
            else:
                result[rel] = ("dir",)
        return result

    def test_build_is_deterministic_and_status_is_read_only(self):
        missing = self.root / "never-created-status-root"
        absent = release.release_status(missing, live_root=self.live)
        self.assertEqual(absent["artifact"]["state"], "absent")
        self.assertFalse(missing.exists())

        first = self._build()
        second = self._build()
        self.assertEqual(first["release_digest"], second["release_digest"])
        self.assertEqual(first["archive_digest"], second["archive_digest"])
        self.assertTrue(second["artifact_reused"])
        manifest = json.loads(
            (Path(first["artifact_dir"]) / "manifest.json").read_text()
        )
        self.assertEqual(manifest["schema"], release.RELEASE_SCHEMA)
        self.assertFalse(manifest["source_dirty"])
        self.assertTrue(manifest["promotable"])
        self.assertLessEqual(
            manifest["build_started_at"], manifest["build_finished_at"]
        )
        self.assertEqual(
            set(manifest["members"]),
            {"core", "tui", "validation", "overlay_live", "overlay_qa"},
        )
        self.assertNotEqual(manifest["qa"]["extension_uuid"], release.LIVE_UUID)
        tampered = json.loads(json.dumps(manifest))
        tampered["source_revision"] = "0" * 40
        with self.assertRaisesRegex(release.ReleaseError, "digest"):
            release._verify_manifest(tampered)
        tampered = json.loads(json.dumps(manifest))
        tampered["promotable"] = False
        with self.assertRaisesRegex(release.ReleaseError, "eligibility"):
            release._verify_manifest(tampered)

        before = self._snapshot(self.state)
        status = release.release_status(self.state, live_root=self.live)
        after = self._snapshot(self.state)
        self.assertEqual(before, after)
        self.assertTrue(status["read_only"])
        self.assertEqual(status["artifact"]["state"], "current")
        self.assertEqual(status["live"]["state"], "absent")
        artifact_signal = self._status_cli("artifact")
        self.assertEqual(
            set(artifact_signal["artifact"]),
            {
                "type",
                "digest",
                "source_revision",
                "build_started_at",
                "build_finished_at",
            },
        )
        self.assertEqual(artifact_signal["artifact"]["digest"], first["release_digest"])

    def test_qa_deployments_are_concurrent_and_leave_live_untouched(self):
        artifact = Path(self._build()["artifact_dir"])
        live_before = self._snapshot(self.home)
        first = release.deploy_qa(self.state, artifact_dir=artifact, qa_id="alice")
        second = release.deploy_qa(self.state, artifact_dir=artifact, qa_id="bob")
        self.assertNotEqual(first["deployment_root"], second["deployment_root"])
        self.assertNotEqual(first["spool_root"], second["spool_root"])
        self.assertNotEqual(first["mcp_address"], second["mcp_address"])
        self.assertNotEqual(first["mcp_server_name"], second["mcp_server_name"])
        self.assertNotEqual(first["extension_uuid"], release.LIVE_UUID)
        self.assertTrue(Path(first["extension_install_root"]).is_symlink())
        self.assertEqual(
            Path(first["extension_install_root"]).resolve(),
            Path(first["extension_root"]).resolve(),
        )
        claude_fragment = json.loads(Path(first["claude_config"]).read_text())
        qa_env = claude_fragment["mcpServers"][first["mcp_server_name"]]["env"]
        self.assertEqual(qa_env["HC_SPOOL"], first["spool_root"])
        self.assertEqual(qa_env["XDG_STATE_HOME"], first["state_root"])
        self.assertIn("xdg-data", qa_env["XDG_DATA_HOME"])
        self.assertEqual(self._snapshot(self.home), live_before)
        self.assertFalse(self.live.exists())
        validation = release.validate_qa(self.state, qa_id="alice")
        self.assertEqual(validation["automated"]["state"], "passed")
        qa_signal = self._status_cli("qa")
        self.assertEqual(qa_signal["deployed_digest"], first["release_digest"])
        self.assertEqual(qa_signal["deployed_revision"], first["source_revision"])
        self.assertIsNotNone(qa_signal["deployed_at"])
        attestation = self._status_cli("attestation")["validation"]
        self.assertEqual(attestation["status"], "pending")
        self.assertEqual(attestation["artifact_digest"], first["release_digest"])
        self.assertEqual(attestation["source_revision"], first["source_revision"])
        self.assertIsNotNone(attestation["test_run_id"])
        self.assertIsNone(attestation["validated_at"])

    def test_qa_extension_payload_resolves_every_relative_import(self):
        artifact = Path(self._build()["artifact_dir"])
        deployment = release.deploy_qa(self.state, artifact_dir=artifact, qa_id="imports")
        qa_base = Path(deployment["extension_root"])
        live_base = (
            Path(deployment["deployment_root"]) / "release/overlay" / release.LIVE_UUID
        )
        live_js = sorted(p.name for p in live_base.glob("*.js"))
        self.assertEqual(sorted(p.name for p in qa_base.glob("*.js")), live_js)
        self.assertIn("palette.js", live_js)
        for path in sorted(qa_base.rglob("*.js")):
            for target in release._relative_js_imports(path.read_text()):
                self.assertTrue(
                    (path.parent / target).is_file(),
                    f"{path.name} imports {target}, absent from the QA payload",
                )
        validation = release.validate_qa(self.state, qa_id="imports")
        gate = next(
            item
            for item in validation["automated"]["checks"]
            if item["name"] == "qa-extension-imports"
        )
        self.assertEqual(gate["state"], "passed", gate)
        self.assertGreaterEqual(gate["scanned_files"], len(live_js))

        # The gate must be able to FAIL: remove the module the parse check
        # cannot miss, and watch it name the unresolvable import.
        (qa_base / "palette.js").unlink()
        broken = release.validate_qa(self.state, qa_id="imports")
        failed = next(
            item
            for item in broken["automated"]["checks"]
            if item["name"] == "qa-extension-imports"
        )
        self.assertEqual(failed["state"], "failed", failed)
        self.assertIn("palette.js", failed["detail"])
        parse = next(
            item
            for item in broken["automated"]["checks"]
            if item["name"] == "qa-extension-parse"
        )
        self.assertEqual(parse["state"], "passed", parse)
        self.assertEqual(broken["automated"]["state"], "failed")
        self.assertEqual(broken["state"], "failed")

    def test_dirty_build_is_explicitly_non_promotable(self):
        with (self.source / "doctrine-on.md").open("a", encoding="utf-8") as handle:
            handle.write("\nDirty QA fixture.\n")
        with self.assertRaisesRegex(release.ReleaseError, "source is dirty"):
            self._build()
        self.assertFalse(self.state.exists())

        build = release.build_release(self.source, self.state, allow_dirty=True)
        self.assertTrue(build["source_dirty"])
        self.assertFalse(build["promotable"])
        deployment, validation = self._automated(
            "dirty", artifact=Path(build["artifact_dir"])
        )
        self.assertEqual(validation["automated"]["state"], "passed")
        self._attest("dirty", deployment["release_digest"])
        with self.assertRaisesRegex(release.ReleaseError, "non-promotable"):
            self._promote("dirty", deployment["release_digest"])

    def test_promotion_refuses_missing_visual_and_mismatched_digest(self):
        deployment, _ = self._automated("candidate")
        with self.assertRaisesRegex(release.ReleaseError, "candidate"):
            self._promote("candidate", deployment["release_digest"])
        wrong = "sha256:" + "0" * 64
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            release.attest_visual(
                self.state,
                qa_root=None,
                qa_id="candidate",
                release_digest=wrong,
                result="passed",
                actor="test",
                note="wrong digest refusal",
            )
        self._attest("candidate", deployment["release_digest"])
        with self.assertRaisesRegex(release.ReleaseError, "not the attested"):
            self._promote("candidate", wrong)

    def test_promotion_installs_exact_bytes_without_building(self):
        deployment, _ = self._automated("exact")
        digest = deployment["release_digest"]
        self._attest("exact", digest)
        qa_release = Path(deployment["release_root"])
        qa_snapshot = self._snapshot(qa_release)
        with mock.patch(
            "release.build_release",
            side_effect=AssertionError("promotion invoked build"),
        ):
            receipt = self._promote("exact", digest)
        self.assertEqual(receipt["deployed_digest"], digest)
        self.assertEqual(receipt["promotion_build_invocations"], 0)
        self.assertEqual(qa_snapshot, self._snapshot(Path(receipt["release_root"])))
        self.assertEqual(
            os.readlink(self.live / "current"), f"releases/{digest.split(':', 1)[1]}"
        )
        for link in ("copilot-tui", "copilot-toggle", "copilot-open"):
            self.assertIn(
                str(self.live / "current"), os.readlink(self.home / "bin" / link)
            )
        responder = self.home / ".local/share/gnome-shell/responder.py"
        self.assertEqual(os.readlink(responder), str(self.live / "current/responder.py"))
        self.assertEqual(responder.read_bytes(), (qa_release / "responder.py").read_bytes())
        self.assertTrue((qa_release / "publisher_lifecycle.py").is_file())
        claude = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(
            claude["mcpServers"]["human-copilot"]["command"],
            str(self.live / "current/server.py"),
        )
        self.assertEqual(claude["mcpServers"]["other"]["command"], "/other/server")
        self.assertIn(
            f'command = "{self.live}/current/server.py"',
            (self.codex_home / "config.toml").read_text(),
        )
        self.assertIn(
            'command = "/other/server"', (self.codex_home / "config.toml").read_text()
        )
        status = release.release_status(self.state, live_root=self.live)
        self.assertEqual(status["live"]["state"], "current")
        self.assertEqual(status["live"]["deployed_digest"], digest)
        attestation = self._status_cli("attestation")["validation"]
        self.assertEqual(attestation["status"], "passed")
        self.assertTrue(attestation["passed"])
        self.assertIsNotNone(attestation["validated_at"])
        production = self._status_cli("production")
        self.assertEqual(production["deployed_digest"], digest)
        self.assertEqual(production["deployed_revision"], receipt["source_revision"])
        self.assertEqual(
            production["deployment_status"],
            "selected; overlay reload not requested",
        )
        self.assertIsNotNone(production["deployed_at"])

    def test_managed_qa_default_trusts_the_registered_live_workspace(self):
        owner = self.root / "owner"
        worktree_script = owner / ".qa/worktrees/qa/release.py"
        with mock.patch.dict(os.environ, {"HC_LIVE_ROOT": ""}):
            self.assertEqual(
                release._default_legacy_source_root(worktree_script),
                owner.resolve(),
            )
        explicit = self.root / "explicit-live"
        with mock.patch.dict(os.environ, {"HC_LIVE_ROOT": str(explicit)}):
            self.assertEqual(
                release._default_legacy_source_root(worktree_script),
                explicit.resolve(),
            )

    def test_active_overlay_records_next_login_without_fake_reload(self):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout="State: ACTIVE\n", stderr=""
            )

        with mock.patch("release.shutil.which", return_value="/usr/bin/gnome-extensions"), \
                mock.patch("release._run", side_effect=fake_run):
            result = release._reload_overlay("auto")
        self.assertEqual(result["state"], "next-login")
        self.assertIn("does not prove", result["reason"])
        self.assertEqual(
            calls,
            [["/usr/bin/gnome-extensions", "info", release.LIVE_UUID]],
        )

    def test_rollback_selects_previous_tested_release_without_building(self):
        first, _ = self._automated("release-a")
        self._attest("release-a", first["release_digest"])
        self._promote("release-a", first["release_digest"])

        with (self.source / "doctrine-off.md").open("a", encoding="utf-8") as handle:
            handle.write("\nRelease B fixture.\n")
        self._git("add", "doctrine-off.md")
        self._git("commit", "-qm", "release B")
        second, _ = self._automated("release-b")
        self.assertNotEqual(first["release_digest"], second["release_digest"])
        self._attest("release-b", second["release_digest"])
        self._promote("release-b", second["release_digest"])

        with mock.patch(
            "release.build_release",
            side_effect=AssertionError("rollback invoked build"),
        ):
            receipt = release.rollback_live(
                self.state,
                confirm="previous",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                reload_overlay="never",
            )
        self.assertEqual(receipt["operation"], "rollback")
        self.assertEqual(receipt["deployed_digest"], first["release_digest"])
        self.assertEqual(receipt["rolled_back_from"], second["release_digest"])
        self.assertEqual(receipt["rollback_build_invocations"], 0)
        expected = f"releases/{first['release_digest'].split(':', 1)[1]}"
        self.assertEqual(os.readlink(self.live / "current"), expected)
        status = release.release_status(self.state, live_root=self.live)
        self.assertEqual(status["live"]["state"], "current")
        self.assertEqual(status["live"]["deployed_digest"], first["release_digest"])

    # ---- rehearsal (hcop-bfw.4) --------------------------------------------
    # promote and rollback rewrite the operator's installed lane. Until now
    # they could only be walked by performing them: the suite sandboxed the
    # transaction with home=/live_root=, but nobody could SEE what a promotion
    # would do to a real desktop without doing it. --dry-run runs every refusal
    # and reports the plan; these pin that it writes nothing and that the plan
    # it prints is the transaction that actually follows.

    def _dry_promote(self, qa_id, digest):
        return release.promote_live(
            self.state,
            qa_id=qa_id,
            release_digest=digest,
            confirm="attested",
            home=self.home,
            codex_home=self.codex_home,
            live_root=self.live,
            legacy_source_root=self.source,
            reload_overlay="never",
            dry_run=True,
        )

    def test_promotion_dry_run_reports_the_plan_and_writes_nothing(self):
        deployment, _ = self._automated("rehearse")
        digest = deployment["release_digest"]
        self._attest("rehearse", digest)

        before_home, before_live = self._snapshot(self.home), self._snapshot(self.live)
        plan = self._dry_promote("rehearse", digest)

        self.assertEqual(self._snapshot(self.home), before_home)
        self.assertEqual(self._snapshot(self.live), before_live)

        self.assertEqual(plan["schema"], "human-copilot-live-plan/v1")
        self.assertEqual(plan["operation"], "promote")
        self.assertTrue(plan["dry_run"] and plan["wrote_nothing"])
        self.assertEqual(plan["would_deploy_digest"], digest)
        self.assertIsNone(plan["previous_digest"])
        self.assertEqual(plan["release_dir"]["state"], "would-create")
        self.assertEqual(plan["reload"]["state"], "not-performed")
        self.assertIn("confirm-token is the literal 'attested'", plan["refusals_checked"])
        self.assertGreaterEqual(len(plan["refusals_checked"]), 8)

        # every owned link is named with the exact target it would be given
        planned = {item["path"]: item["to"] for item in plan["links"]}
        for name in ("copilot-tui", "copilot-toggle", "copilot-open"):
            self.assertIn(str(self.live / "current"), planned[str(self.home / "bin" / name)])
        self.assertEqual(len(planned), 5)
        self.assertIn(str(self.home / ".local/share/gnome-shell/responder.py"), planned)
        self.assertTrue(all(item["changes"] for item in plan["links"]))

        # and both config files, with whether each would actually change
        self.assertEqual(
            plan["registrations"]["claude"]["path"], str(self.home / ".claude.json")
        )
        self.assertEqual(
            plan["registrations"]["codex"]["path"], str(self.codex_home / "config.toml")
        )
        self.assertTrue(plan["registrations"]["claude"]["changes"])

        # THE POINT: the rehearsal predicted the transaction that follows
        receipt = self._promote("rehearse", digest)
        self.assertEqual(receipt["deployed_digest"], plan["would_deploy_digest"])
        self.assertEqual(receipt["manifest_digest"], plan["manifest_digest"])
        self.assertEqual(receipt["previous_digest"], plan["previous_digest"])
        self.assertEqual(
            os.readlink(self.live / "current"), plan["current_link"]["to"]
        )
        for item in plan["links"]:
            self.assertEqual(os.readlink(Path(item["path"])), item["to"])
        claude = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(
            claude["mcpServers"]["human-copilot"]["command"],
            str(self.live / "current/server.py"),
        )

        # a second rehearsal, now that LIVE exists, still writes nothing
        after = self._snapshot(self.home), self._snapshot(self.live)
        again = self._dry_promote("rehearse", digest)
        self.assertEqual((self._snapshot(self.home), self._snapshot(self.live)), after)
        self.assertEqual(again["release_dir"]["state"], "present-and-verified")
        self.assertFalse(any(item["changes"] for item in again["links"]))
        self.assertFalse(again["registrations"]["claude"]["changes"])

    def test_dry_run_still_raises_every_real_refusal(self):
        deployment, _ = self._automated("refuse")
        digest = deployment["release_digest"]

        # a rehearsal is not a pass generator: the visual gate refuses first
        with self.assertRaises(release.ReleaseError):
            self._dry_promote("refuse", digest)
        self._attest("refuse", digest)
        self._dry_promote("refuse", digest)

        # wrong confirm token
        with self.assertRaises(release.ReleaseError):
            release.promote_live(
                self.state,
                qa_id="refuse",
                release_digest=digest,
                confirm="no",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                legacy_source_root=self.source,
                dry_run=True,
            )

        # an owned path that is a real file, not a symlink we may replace
        hijacked = self.home / "bin/copilot-tui"
        hijacked.parent.mkdir(parents=True, exist_ok=True)
        if hijacked.is_symlink():
            hijacked.unlink()
        hijacked.write_text("not ours\n", encoding="utf-8")
        before = self._snapshot(self.home)
        with self.assertRaises(release.ReleaseError) as caught:
            self._dry_promote("refuse", digest)
        self.assertIn("non-symlink", str(caught.exception))
        self.assertEqual(self._snapshot(self.home), before)
        hijacked.unlink()

        # nothing to roll back to yet
        with self.assertRaises(release.ReleaseError):
            release.rollback_live(
                self.state,
                confirm="previous",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                reload_overlay="never",
                dry_run=True,
            )

    def test_rollback_dry_run_reports_the_plan_and_writes_nothing(self):
        first, _ = self._automated("back-a")
        self._attest("back-a", first["release_digest"])
        self._promote("back-a", first["release_digest"])
        with (self.source / "doctrine-off.md").open("a", encoding="utf-8") as handle:
            handle.write("\nRehearsal fixture B.\n")
        self._git("add", "doctrine-off.md")
        self._git("commit", "-qm", "rehearsal B")
        second, _ = self._automated("back-b")
        self._attest("back-b", second["release_digest"])
        self._promote("back-b", second["release_digest"])

        before_home, before_live = self._snapshot(self.home), self._snapshot(self.live)
        plan = release.rollback_live(
            self.state,
            confirm="previous",
            home=self.home,
            codex_home=self.codex_home,
            live_root=self.live,
            reload_overlay="never",
            dry_run=True,
        )
        self.assertEqual(self._snapshot(self.home), before_home)
        self.assertEqual(self._snapshot(self.live), before_live)

        self.assertEqual(plan["operation"], "rollback")
        self.assertTrue(plan["dry_run"] and plan["wrote_nothing"])
        self.assertEqual(plan["would_deploy_digest"], first["release_digest"])
        self.assertEqual(plan["rolled_back_from"], second["release_digest"])
        self.assertEqual(plan["links"], [])
        self.assertIn("confirm-token is the literal 'previous'", plan["refusals_checked"])

        receipt = release.rollback_live(
            self.state,
            confirm="previous",
            home=self.home,
            codex_home=self.codex_home,
            live_root=self.live,
            reload_overlay="never",
        )
        self.assertEqual(receipt["deployed_digest"], plan["would_deploy_digest"])
        self.assertEqual(receipt["rolled_back_from"], plan["rolled_back_from"])
        self.assertEqual(os.readlink(self.live / "current"), plan["current_link"]["to"])

    def test_dry_run_creates_nothing_on_a_desktop_with_no_release_state(self):
        """The rehearsal's first act must not be a write. _locked() mkdirs the
        state root and opens its lock file, so routing --dry-run through it
        would leave a directory on a machine that has never built a release --
        measured on the real desktop 2026-09-03, which is how this was found."""
        virgin = self.root / "never-built"
        self.assertFalse(virgin.exists())
        with self.assertRaises(release.ReleaseError):
            release.promote_live(
                virgin,
                confirm="attested",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                legacy_source_root=self.source,
                dry_run=True,
            )
        self.assertFalse(virgin.exists(), "a rehearsal created the release state root")
        with self.assertRaises(release.ReleaseError):
            release.rollback_live(
                virgin,
                confirm="previous",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                dry_run=True,
            )
        self.assertFalse(virgin.exists(), "a rehearsal created the release state root")
        # the real path still creates and locks it
        with self.assertRaises(release.ReleaseError):
            release.promote_live(
                virgin,
                confirm="attested",
                home=self.home,
                codex_home=self.codex_home,
                live_root=self.live,
                legacy_source_root=self.source,
            )
        self.assertTrue((virgin / ".release.lock").exists())

    def test_dry_run_reaches_the_plan_through_the_command_line(self):
        deployment, _ = self._automated("cli-rehearse")
        digest = deployment["release_digest"]
        self._attest("cli-rehearse", digest)
        before_home, before_live = self._snapshot(self.home), self._snapshot(self.live)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = release.main(
                [
                    "--state-root", str(self.state),
                    "promote",
                    "--qa-id", "cli-rehearse",
                    "--release-digest", digest,
                    "--confirm-live", "attested",
                    "--home", str(self.home),
                    "--codex-home", str(self.codex_home),
                    "--live-root", str(self.live),
                    "--legacy-source-root", str(self.source),
                    "--reload-overlay", "never",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        plan = json.loads(output.getvalue())
        self.assertTrue(plan["dry_run"] and plan["wrote_nothing"])
        self.assertEqual(plan["would_deploy_digest"], digest)
        self.assertEqual(self._snapshot(self.home), before_home)
        self.assertEqual(self._snapshot(self.live), before_live)



if __name__ == "__main__":
    unittest.main()
