"""Legacy acceptance commands must not depend on the operator's surface policy."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import selftest
import toggle


class StandaloneSelftestTests(unittest.TestCase):
    def test_original_commands_pass_with_disabled_overlay_only_parent(self):
        commands = (
            "selftest.test_instance_presence(); selftest.test_sidepanel(); "
            "selftest.test_instance_launch_wait()",
            "selftest.test_server(); selftest.test_instance_stamp(); "
            "selftest.test_publish_request()",
        )
        with tempfile.TemporaryDirectory(prefix="hc-parent-policy-") as directory:
            root = Path(directory)
            control = root / "control.json"
            surface = root / "surface.json"
            selftest.write_control_fixture(str(control), False)
            surface.write_text(json.dumps({"mode": "overlay"}))
            originals = {path: path.read_bytes() for path in (control, surface)}
            env = {**os.environ, "HC_CONTROL_FILE": str(control),
                   "HC_SURFACE_POLICY_FILE": str(surface)}
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-c", "import selftest; " + command],
                        cwd=Path(selftest.__file__).parent, env=env,
                        capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0,
                                     result.stdout + result.stderr)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_fixture_restores_environment_and_probe_after_failure(self):
        before = dict(os.environ)
        probe = toggle._run_probe
        fixture_paths = []

        @selftest._isolated_legacy_server_test
        def failing_check():
            fixture_paths.append(Path(os.environ["HC_CONTROL_FILE"]).parent)
            self.assertTrue(toggle.capability_enabled())
            self.assertEqual(json.loads(Path(os.environ["HC_SURFACE_POLICY_FILE"])
                                        .read_text()), {"mode": "auto"})
            raise RuntimeError("fixture cleanup sentinel")

        with self.assertRaisesRegex(RuntimeError, "fixture cleanup sentinel"):
            failing_check()
        self.assertEqual(dict(os.environ), before)
        self.assertIs(toggle._run_probe, probe)
        self.assertTrue(fixture_paths)
        self.assertTrue(all(not path.exists() for path in fixture_paths))


if __name__ == "__main__":
    unittest.main()
