#!/usr/bin/env python3
"""Explicit staged Mac-arm64 -> Windows-x64 build, using an existing source archive.

Default is PLAN ONLY. --run must be launched under the operator's OS-pressure/disk
process-group guard (taskpolicy utility QoS / nice 19). This script never starts a daemon,
installs global packages, changes system proxies, or publishes/installs a browser.

Example (create the private job directory first):
  python3 scripts/guard_mac_build.py --directory ~/clearcote-ime-spike/build-job \
    --max-hours 48 -- python3 /absolute/repo/scripts/mac_build.py --stage all \
    --run --json ~/clearcote-ime-spike/build-result.json

Stages stop at the first failure. Failed source/patch stages require inspection,
not a blind re-apply to a partially mutated tree. Build artifacts remain pending
Windows IME runtime acceptance; the cross-build does not run blink_unittests.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

from check_ime_patch import transient
from mac_clt_sdk import metadata
from prepare_mac_clt import prepare
from prepare_mac_winsdk import layout

REPO = Path(__file__).resolve().parents[1]
STAGES = ("source", "patch", "toolchains", "bootstrap", "configure", "build", "package")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((json.dumps(value, indent=2) + "\n").encode())
    temporary.replace(path)


class Build:
    def __init__(self, args):
        self.args = args
        self.work = args.work.expanduser().resolve()
        self.src = self.work / "build/src"
        self.sdk = args.toolchain.expanduser().resolve()
        self.redist = args.vcredist.expanduser().resolve()
        self.pins = json.loads((REPO / "tools/mac-clt/build-pins.json").read_text())
        requested_jobs = getattr(args, "jobs", None)
        requested_links = getattr(args, "link_threads", None)
        self.jobs = (os.cpu_count() or 1) if requested_jobs is None else requested_jobs
        self.link_threads = self.jobs if requested_links is None else requested_links
        if not 1 <= self.jobs <= 256 or not 1 <= self.link_threads <= 256:
            raise ValueError("jobs/link threads must be between 1 and 256")
        self.state_path = self.work / "mac-build-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"stages": {}, "downloads": []}
        # A checkpoint belongs to these exact inputs, never just a stage name.
        inputs = [REPO / "tools/mac-clt/build-pins.json", REPO / "config/args.gn",
                  REPO / "tools/mac-clt/host-sdk.patch", REPO / "tools/mac-clt/link-threads.patch",
                  REPO / "scripts/mac_clt_sdk.py", REPO / "scripts/mac_build.py",
                  REPO / "patches/series"]
        inputs += [REPO / "patches" / line.split("#")[0].strip()
                   for line in (REPO / "patches/series").read_text().splitlines()
                   if line.split("#")[0].strip()]
        self.identity = {str(path.relative_to(REPO)): sha(path) for path in inputs}
        if self.state.get("identity", self.identity) != self.identity:
            raise RuntimeError("build inputs changed since the checkpoint; inspect before reusing this private tree")
        self.state["identity"] = self.identity
        self.env = {**os.environ, "TARGET": "windows", "WORK": str(self.work), "REPO": str(REPO),
                    "CLEARCOTE_SKIP_PATCH_VERIFY": "0", "PYTHONUNBUFFERED": "1",
                    "PYTHONUTF8": "1", "XZ_DEFAULTS": "-T1"}
        self.env["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        self.info = None

    def execute(self, command, *, cwd=None, env=None, network=False):
        print("RUN " + shlex.join(map(str, command)), flush=True)
        attempts = 4 if network else 1
        for attempt in range(attempts):
            # Network operations are finite and captured to classify transient errors.
            try:
                result = subprocess.run(list(map(str, command)), cwd=cwd or self.work, env=env or self.env,
                                        text=True, stdout=subprocess.PIPE if network else None,
                                        stderr=subprocess.STDOUT if network else None,
                                        timeout=180 if network else None)
            except subprocess.TimeoutExpired:
                if not network or attempt + 1 == attempts:
                    raise
                print(f"network command timeout; retry {attempt + 1}/3", flush=True)
                time.sleep(2 ** attempt)
                continue
            if not result.returncode:
                if network and result.stdout:
                    print(result.stdout, flush=True)
                return result
            text = result.stdout or ""
            retryable = bool(re.search(r"EOF|reset|timed? out|HTTP (?:408|429|50[0234])|Could not resolve|Failed to connect", text, re.I))
            if not network or not retryable or attempt + 1 == attempts:
                raise RuntimeError(f"command failed ({result.returncode}): {text}")
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def checkout(self, pin: dict, directory: Path):
        if not (directory / ".git").exists():
            directory.mkdir(parents=True, exist_ok=True)
            if any(directory.iterdir()):
                raise RuntimeError(f"refusing to replace nonempty unregistered dependency: {directory}")
            self.execute(["git", "init", directory])
            self.execute(["git", "-C", directory, "remote", "add", "origin", pin["url"]])
        current = subprocess.run(["git", "-C", directory, "rev-parse", "HEAD"], capture_output=True, text=True)
        if current.returncode or current.stdout.strip() != pin["commit"]:
            self.execute(["git", "-C", directory, "-c", "http.version=HTTP/1.1", "fetch", "--depth=1", "origin", pin["commit"]], network=True)
            self.execute(["git", "-C", directory, "checkout", "--detach", "FETCH_HEAD"])
        actual = subprocess.check_output(["git", "-C", directory, "rev-parse", "HEAD"], text=True).strip()
        if actual != pin["commit"]:
            raise RuntimeError("dependency revision mismatch")

    def download(self, url: str, output):
        """Finite transport retries; integrity checks are outside retries."""
        for attempt in range(4):
            try:
                output.seek(0)
                output.truncate()
                digest = hashlib.sha256()
                count = 0
                with urllib.request.urlopen(url, timeout=60) as response:
                    expected = int(response.headers["Content-Length"]) if response.headers.get("Content-Length") else None
                    while True:
                        chunk = response.read(128 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                    if expected is not None and count != expected:
                        raise http.client.IncompleteRead(b"", expected - count)
                self.state["downloads"].append({"url": url, "sha256": digest.hexdigest(), "size": count})
                write_json(self.state_path, self.state)
                output.seek(0)
                return
            except Exception as error:
                if attempt == 3 or not transient(error):
                    raise
                print(f"transient download: {type(error).__name__}; retry {attempt + 1}/3", flush=True)
                time.sleep(2 ** attempt)

    def source(self):
        archive = self.work / "download_cache" / self.pins["archive"]["name"]
        expected = self.pins["archive"]
        if archive.stat().st_size != expected["size"] or sha(archive) != expected["sha256"]:
            raise RuntimeError("source archive incomplete or SHA256 mismatch; resume the separately guarded source download")
        if self.src.exists():
            raise RuntimeError("source tree already exists without a completed source checkpoint; inspect before unpacking again")
        self.checkout(self.pins["ungoogled"], self.work / "ungoogled-chromium")
        self.checkout(self.pins["windows_overlay"], self.work / "ungoogled-chromium-windows")
        ug = self.work / "ungoogled-chromium"
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.execute([sys.executable, ug / "utils/downloads.py", "unpack", "-c", self.work / "download_cache",
                      "-i", ug / "downloads.ini", "--", self.src])
        self.execute([sys.executable, ug / "utils/prune_binaries.py", self.src, ug / "pruning.list"])
        for dependency in self.pins["windows_deps"]:
            self.checkout(dependency, self.src / dependency["path"])
        self.check_source_version()

    def check_source_version(self):
        version = dict(line.split("=", 1) for line in (self.src / "chrome/VERSION").read_text().splitlines() if "=" in line)
        actual = ".".join(version[key].strip() for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))
        if actual != self.pins["chromium_version"]:
            raise RuntimeError(f"checkpoint source version mismatch: {actual}")

    def gate(self):
        self.execute([sys.executable, REPO / "scripts/verify_patches.py", "--tree", self.src, "--target", "windows"])

    def patch(self):
        self.execute(["bash", REPO / "scripts/01-apply-patches.sh"])
        write_json(self.work / "host-sdk-adapter.json", prepare(self.src, False))
        patch = REPO / "tools/mac-clt/link-threads.patch"
        self.execute(["git", "apply", "--check", patch], cwd=self.src)
        self.execute(["git", "apply", patch], cwd=self.src)
        self.execute(["git", "apply", "--reverse", "--check", patch], cwd=self.src)
        write_json(self.work / "windows-sdk-layout.json", layout(self.sdk))
        self.gate()

    def updater(self, path: Path, name: str, arguments: list[str]):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # Preserve pinned updater extraction/stamp logic; only transport gets bounds.
        if name == "update":
            module.DownloadUrl = self.download
        saved = sys.argv
        try:
            sys.argv = [str(path), *arguments]
            status = module.main()
            if status not in (None, 0):
                raise RuntimeError(f"pinned updater failed: {path.name} ({status})")
        finally:
            sys.argv = saved

    def toolchains(self):
        gclient = self.src / ".gclient"
        payload = b"target_os = ['win', 'mac']\n"
        if gclient.exists() and gclient.read_bytes() != payload:
            raise RuntimeError("refusing to overwrite an unknown .gclient configuration")
        gclient.write_bytes(payload)
        socket.setdefaulttimeout(60)
        self.updater(self.src / "tools/clang/scripts/update.py", "update", ["--host-os", "mac-arm64"])
        self.updater(self.src / "tools/rust/update_rust.py", "clearcote_update_rust", [])
        # The official updater downloads four platforms and assumes GNU wget/sha256sum.
        # Download just the pinned host node binary to node.py's actual arm64 path.
        script = (self.src / "third_party/node/update_node_binaries").read_text()
        version = re.search(r'NODE_VERSION="(v[0-9.]+)"', script).group(1)
        asset = f"node-{version}-darwin-arm64.tar.gz"
        base = f"https://nodejs.org/dist/{version}/"
        with tempfile.TemporaryFile() as checks:
            self.download(base + "SHASUMS256.txt", checks)
            lines = checks.read().decode().splitlines()
        expected = next(line.split()[0] for line in lines if line.split()[-1] == asset)
        destination = self.src / "third_party/node/mac_arm64/node-darwin-arm64/bin/node"
        with tempfile.TemporaryFile() as archive:
            self.download(base + asset, archive)
            actual = hashlib.file_digest(archive, "sha256").hexdigest()
            if actual != expected:
                raise RuntimeError("Node archive SHA256 mismatch; not retrying an integrity failure")
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:gz") as package:
                member = package.getmember(f"node-{version}-darwin-arm64/bin/node")
                data = package.extractfile(member).read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o755)
        self.execute([destination, "--version"])
        for tool in ("clang", "clang++", "clang-cl", "lld-link", "ld64.lld", "llvm-ar"):
            candidate = self.src / "third_party/llvm-build/Release+Asserts/bin" / tool
            if not candidate.is_file():
                raise RuntimeError(f"native LLVM tool missing: {tool}")
            description = subprocess.check_output(["file", "-L", candidate], text=True)
            if "Mach-O" not in description or "arm64" not in description:
                raise RuntimeError(f"LLVM tool is not native Mac arm64: {description}")
        self.execute([self.src / "third_party/rust-toolchain/bin/rustc", "--version"])

    def bootstrap(self):
        llvm = self.src / "third_party/llvm-build/Release+Asserts/bin"
        wrapper = self.work / "bin/ninja-background"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_bytes(f'#!/bin/sh\nexec /opt/homebrew/bin/ninja "$@" -j{self.jobs}\n'.encode())
        wrapper.chmod(0o755)
        sdk = metadata()["sdk_path"]
        environment = {**self.env, "PATH": str(llvm) + ":" + self.env["PATH"],
                       "CC": str(llvm / "clang"), "CXX": str(llvm / "clang++"),
                       "AR": str(llvm / "llvm-ar"), "NINJA": str(wrapper),
                       "CFLAGS": "-isysroot " + shlex.quote(sdk),
                       "CXXFLAGS": "-isysroot " + shlex.quote(sdk),
                       "LDFLAGS": f"-fuse-ld=lld -Wl,--threads={self.link_threads} -isysroot " + shlex.quote(sdk)}
        self.execute([sys.executable, self.src / "tools/gn/bootstrap/bootstrap.py",
                      "--skip-generate-buildfiles", "--jobs", self.jobs, "--output", self.work / "bin/gn"], cwd=self.src, env=environment)
        self.execute([self.work / "bin/gn", "--version"])

    def configured_env(self):
        return {**self.env, "DEPOT_TOOLS_WIN_TOOLCHAIN": "1", "GYP_MSVS_OVERRIDE_PATH": str(self.sdk),
                "WINDOWSSDKDIR": str(self.sdk / "Windows Kits/10"), "WDK_DIR": ""}

    def configure(self):
        self.gate()
        out = self.src / "out/MacWinIme"
        out.mkdir(parents=True, exist_ok=True)
        extra = f"\nclearcote_clt_host_sdk=true\nuse_system_xcode=true\nclearcote_host_link_threads={self.link_threads}\nconcurrent_links=1\nuse_thin_lto=false\nuse_siso=false\n"
        (out / "args.gn").write_bytes((REPO / "config/args.gn").read_bytes() + extra.encode())
        self.execute([self.work / "bin/gn", "gen", out, f"--threads={self.jobs}"], cwd=self.src, env=self.configured_env())

    def build(self):
        self.execute(["ninja", "-C", self.src / "out/MacWinIme", f"-j{self.jobs}", "chrome"], cwd=self.src, env=self.configured_env())

    def package(self):
        self.gate()
        out = self.src / "out/MacWinIme"
        required = ["chrome.exe", "chrome.dll", "chrome_elf.dll", "chrome_wer.dll", "icudtl.dat",
                    "libEGL.dll", "libGLESv2.dll", "d3dcompiler_47.dll"]
        runtime = ["concrt140.dll", "msvcp140.dll", "ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll"]
        for file in [out / name for name in required] + [self.redist / name for name in runtime]:
            if not file.is_file():
                raise RuntimeError(f"required packaging file missing: {file}")
        if not list(out.glob("*.manifest")) or not (out / "locales").is_dir():
            raise RuntimeError("Windows SxS manifest/locales missing")
        files = set(out / name for name in required)
        for pattern in ("*.manifest", "*.pak", "*.bin", "*.dat", "*.json", "*.dll"):
            files.update(out.glob(pattern))
        files.update(path for path in (out / "locales").rglob("*") if path.is_file())
        files.difference_update(out / name for name in runtime)
        dist = self.work / "dist"
        dist.mkdir(exist_ok=True)
        archive = dist / f"clearcote-{self.pins['chromium_version']}-windows-x64-ime-candidate.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as package:
            for file in sorted(files):
                package.write(file, file.relative_to(out).as_posix())
            for name in runtime:
                package.write(self.redist / name, name)
        archive_hash = sha(archive)
        (dist / "SHA256SUMS.txt").write_bytes((archive_hash + "  " + archive.name + "\n" +
                                               sha(out / "chrome.exe") + "  chrome.exe\n").encode())
        self.execute([sys.executable, REPO / "scripts/verify_patches.py", "--binary", archive])
        series = [line.split("#")[0].strip() for line in (REPO / "patches/series").read_text().splitlines()]
        provenance = {"status": "candidate built; Windows IME runtime acceptance NOT run",
                      "clearcote_commit": subprocess.check_output(["git", "-C", REPO, "rev-parse", "HEAD"], text=True).strip(),
                      "pins": self.pins, "mandatory_patches": {name: sha(REPO / "patches" / name) for name in series if name},
                      "host_patches": {name: sha(REPO / "tools/mac-clt" / name) for name in ("host-sdk.patch", "link-threads.patch")},
                      "args_gn_sha256": sha(out / "args.gn"), "archive_sha256": archive_hash,
                      "binary_sha256": {name: sha(out / name) for name in ("chrome.exe", "chrome.dll")},
                      "runtime_dll_sha256": {name: sha(self.redist / name) for name in runtime},
                      "sdk": metadata(), "downloads": self.state["downloads"],
                      "scheduling": self.state.get("process_limits"),
                      "resource_guard": {"memory_pressure_provider": "kern.memorystatus_vm_pressure_level",
                                         "qos": "utility", "fixed_rss_limit": None,
                                         "user_activity_detection": False},
                      "cpp_regression": "retained in source; not built/run by chrome cross-build",
                      "runtime_command": "python scripts/ime_runtime_probe.py --binary <extracted/chrome.exe> --json evidence.json"}
        write_json(dist / "provenance.json", provenance)

    def run(self):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("execution requires native macOS arm64; this is not the Linux pipeline")
        root = (Path.home() / "clearcote-ime-spike").resolve()
        for path in (self.work, self.sdk, self.redist):
            if not path.is_relative_to(root):
                raise RuntimeError("all build paths must stay below ~/clearcote-ime-spike")
        if os.getpriority(os.PRIO_PROCESS, 0) < 19:
            raise RuntimeError("run under the external resource guard with nice 19")
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
        self.state["process_limits"] = {"nofile_soft": limit, "nofile_hard": hard,
                                        "nice": os.getpriority(os.PRIO_PROCESS, 0), "ninja_jobs": self.jobs,
                                        "gn_threads": self.jobs, "lld_threads": self.link_threads,
                                        "concurrent_links": 1, "qos": "utility",
                                        "user_activity_detection": False}
        self.work.mkdir(parents=True, exist_ok=True)
        stages = STAGES if self.args.stage == "all" else (self.args.stage,)
        for name in stages:
            previous = self.state["stages"].get(name)
            if previous == "completed":
                self.check_source_version()
                if name in ("patch", "configure", "build", "package"):
                    self.gate()
                print(f"CHECKPOINT {name} already completed", flush=True)
                continue
            if previous in ("started", "failed") and name in ("source", "patch"):
                raise RuntimeError(f"{name} was interrupted/failed; inspect the private partial tree before resuming")
            index = STAGES.index(name)
            if index and self.state["stages"].get(STAGES[index - 1]) != "completed":
                raise RuntimeError(f"complete {STAGES[index - 1]} before {name}")
            if index:
                self.check_source_version()
            self.state["stages"][name] = "started"
            write_json(self.state_path, self.state)
            try:
                getattr(self, name)()
            except Exception:
                self.state["stages"][name] = "failed"
                write_json(self.state_path, self.state)
                raise
            self.state["stages"][name] = "completed"
            write_json(self.state_path, self.state)
        return {"status": "requested stages completed; consult checkpoints, not a business/runtime acceptance", "state": self.state}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path.home() / "clearcote-ime-spike"
    parser.add_argument("--work", type=Path, default=root / "work")
    parser.add_argument("--toolchain", type=Path, default=root / "wintoolchain")
    parser.add_argument("--vcredist", type=Path, default=root / "work/vcredist")
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="Ninja/GN/bootstrap workers; default logical CPUs")
    parser.add_argument("--link-threads", type=int, help="LLD threads; default same as --jobs; concurrent links remains one")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    try:
        build = Build(args)
        result = build.run() if args.run else {"status": "plan only; no build action executed", "stages": STAGES,
            "work": str(build.work), "pins": build.pins,
            "scheduling": {"jobs": build.jobs, "link_threads": build.link_threads, "concurrent_links": 1},
            "constraints": "external OS-pressure/disk guard, nice19; full-series L1 mandatory; no user-activity detection",
            "acceptance": "GN/build not yet executed; Windows runtime and C++ regression remain unverified"}
        status = 0
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
        status = 1
    write_json(args.json.expanduser(), result)
    print(json.dumps(result, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
