"""Lightweight failure-boundary checks: never download or build Chromium."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mac_build


class BuildBoundaryTests(unittest.TestCase):
    def make_build(self, root):
        return mac_build.Build(argparse.Namespace(work=root / "work", toolchain=root / "sdk",
                                                 vcredist=root / "redist", stage="all", run=False))

    def test_default_plan_has_no_build_or_work_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "plan.json"
            with patch.object(sys, "argv", ["mac_build.py", "--work", str(root / "work"), "--json", str(report)]), \
                 patch.object(mac_build.Build, "run", side_effect=AssertionError("must not run")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mac_build.main(), 0)
            self.assertFalse((root / "work").exists())
            self.assertIn("plan only", json.loads(report.read_text())["status"])

    def test_network_timeouts_stop_after_four_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            with patch.object(mac_build.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 180)) as run, \
                 patch.object(mac_build.time, "sleep") as wait, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(subprocess.TimeoutExpired):
                    build.execute(["git", "fetch"], network=True)
            self.assertEqual(run.call_count, 4)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 2, 4])

    def test_permanent_network_command_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            error = subprocess.CompletedProcess([], 128, "fatal: repository not found")
            with patch.object(mac_build.subprocess, "run", return_value=error) as run, \
                 patch.object(mac_build.time, "sleep") as wait, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    build.execute(["git", "fetch"], network=True)
            self.assertEqual(run.call_count, 1)
            wait.assert_not_called()

    def test_corrupt_archive_stops_before_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            archive = build.work / "download_cache" / build.pins["archive"]["name"]
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"wrong archive")
            with patch.object(build, "checkout") as checkout:
                with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                    build.source()
            checkout.assert_not_called()
            self.assertFalse(build.src.exists())

    def test_changed_inputs_reject_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = self.make_build(root)
            state = {"stages": {"source": "completed"}, "downloads": [], "identity": {"stale": "hash"}}
            mac_build.write_json(build.state_path, state)
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                self.make_build(root)


if __name__ == "__main__":
    unittest.main()
