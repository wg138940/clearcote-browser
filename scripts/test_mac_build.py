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

    def test_corrupt_message_golden_archive_cannot_mutate_source(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            archive = build.work / "download_cache/chromium-mc-f405107495a07cb1bfcf687d4af8d91117098db6.tar.gz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"corrupt golden resources")
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                build.ensure_mc_outputs()
            self.assertFalse(build.src.exists())

    def test_requested_concurrency_reaches_gn_ninja_and_lld(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(work=root / "work", toolchain=root / "sdk", vcredist=root / "redist",
                                      stage="all", run=False, jobs=10, link_threads=10)
            build = mac_build.Build(args)
            metadata = build.src / "build/toolchain/win/win_toolchain_data.gni"
            metadata.parent.mkdir(parents=True)
            metadata.write_bytes(b"fixture")
            real_sha = mac_build.sha
            with patch.object(mac_build, "sha", side_effect=lambda path: "51f4425cac2d1b334f8e7b72f199b7674f9d1318430073c16843c2aba91007e3" if path == metadata else "43c080d1dee244820c45cfc84b0f4e96982f9a2d19b8524944c2a43b0a573abc" if path == metadata.with_name("toolchain.gni") else "e9cf2bbb560d31be8d923732fc98f4d63aecd08f344f45524c89dc3159ea5823" if path == build.src / "build/rust/rust_bindgen_generator.gni" else real_sha(path)), \
                 patch.object(build, "gate"), patch.object(build, "ensure_uia_sdk"), patch.object(build, "execute") as execute, \
                 patch.object(mac_build, "layout", return_value={}), \
                 patch.object(mac_build.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):

                build.configure()
                self.assertIn("--threads=10", execute.call_args.args[0])
                text = (build.src / "out/MacWinIme/args.gn").read_text(encoding="utf-8")
                self.assertIn("clearcote_host_link_threads=10", text)
                self.assertIn("concurrent_links=1", text)
                self.assertIn("enable_precompiled_headers=false", text)
                with patch.object(build, "ensure_esbuild"), patch.object(build, "ensure_rc"), patch.object(build, "ensure_midl_outputs"), patch.object(build, "ensure_mc_outputs"):
                    build.build()
                self.assertIn("-j10", execute.call_args.args[0])

    def test_patch_overlay_selector_does_not_leak_into_bindgen_build_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            self.assertEqual(build.env["TARGET"], "windows")
            self.assertNotIn("TARGET", build.configured_env())
            self.assertEqual(build.configured_env()["DEPOT_TOOLS_WIN_TOOLCHAIN"], "1")

    def test_unknown_sdk_header_is_not_overwritten_or_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            build = self.make_build(Path(directory))
            header = build.sdk / "Windows Kits/10/Include/10.0.26100.0/um/UIAutomationCore.h"
            header.parent.mkdir(parents=True)
            header.write_bytes(b"unknown operator SDK")
            with patch.object(build, "download") as download:
                with self.assertRaisesRegex(RuntimeError, "refusing to replace unknown SDK"):
                    build.ensure_uia_sdk()
            download.assert_not_called()
            self.assertEqual(header.read_bytes(), b"unknown operator SDK")

    def test_zero_concurrency_is_rejected_instead_of_silently_defaulting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for jobs, links in ((0, 10), (10, 0)):
                args = argparse.Namespace(work=root / "work", toolchain=root / "sdk", vcredist=root / "redist",
                                          stage="all", run=False, jobs=jobs, link_threads=links)
                with self.assertRaisesRegex(ValueError, "between 1 and 256"):
                    mac_build.Build(args)


if __name__ == "__main__":
    unittest.main()
