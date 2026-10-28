#!/usr/bin/env python3
"""Prepare only the optional CLT host-SDK adapter in a private Chromium tree.

python scripts/prepare_mac_clt.py --source ~/clearcote-ime-spike/work/build/src \
  --json ~/clearcote-ime-spike/host-adapter.json
--check-only validates the exact pinned input/patch without changing the tree.

This does not download sources/toolchains, install packages, run GN/Ninja,
prepare the Windows SDK, or bypass the complete patches/series L1 gate. Run real
configuration/build stages under the operator's idle/resource process guard.
The existing Linux 02/03/04 scripts are NOT a Mac build entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from mac_clt_sdk import metadata

REPO = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source: Path, check_only: bool) -> dict:
    source = source.expanduser().resolve(strict=True)
    pin = json.loads((REPO / "tools/mac-clt/source-pin.json").read_text())
    if pin["chromium_version"] != (REPO / "UPSTREAM_REVISION").read_text().strip():
        raise RuntimeError("CLT adapter pin differs from UPSTREAM_REVISION")
    target = source / pin["path"]
    patch = REPO / "tools/mac-clt/host-sdk.patch"
    helper = REPO / "scripts/mac_clt_sdk.py"
    copied_helper = source / "build/config/mac/clearcote_clt_sdk.py"
    current = digest(target)
    if current not in (pin["pristine_sha256"], pin["patched_sha256"]):
        raise RuntimeError("mac_sdk.gni does not match the exact pinned pristine/patched source")
    if copied_helper.exists() and digest(copied_helper) != digest(helper):
        raise RuntimeError("refusing to overwrite an unknown CLT helper in the build tree")
    args = ["git", "apply", "--check"]
    if current == pin["patched_sha256"]:
        args.append("--reverse")
    subprocess.run([*args, str(patch)], cwd=source, check=True)
    result = {"chromium_version": pin["chromium_version"], "source": str(source),
              "scope": "optional host adapter only; not a full configure/build or L1 result",
              "patch_sha256": digest(patch), "helper_sha256": digest(helper),
              "pristine_sha256": pin["pristine_sha256"], "patched_sha256": pin["patched_sha256"],
              "required_gn_args": ["clearcote_clt_host_sdk=true", "use_system_xcode=true",
                                   "target_os=\"win\"", "is_official_build=false", "use_remoteexec=false"],
              "remaining_stages": [
                  "Fetch/prune the complete pinned Chromium, ungoogled base and Windows overlay; apply every Clearcote series patch and retain the full-series L1 gate.",
                  "Fetch the native mac-arm64 Chromium clang/lld, Rust and Node toolchains; bootstrap GN with a single Ninja job. The CLT SDK alone is not these toolchains.",
                  "Stage the matching Windows SDK/MSVC Include/Lib and SetEnv layout from the operator's installed toolchain; Linux xwin/ciopfs scripts do not run on macOS unchanged.",
                  "Generate the Windows x64 build graph with the explicit CLT opt-in and real Mac host SDK; prove GN configuration succeeds before claiming readiness.",
                  "Build under the idle/resource process guard with Ninja -j1 and bounded linker threads; no parallel bootstrap/link/download jobs may bypass the guard.",
                  "Package the Windows runtime DLLs and SxS manifest, record source/patch/toolchain hashes, and run the supplied binary through the Windows IME runtime probe. The C++ regression has not run."
              ]}
    if check_only:
        return {**result, "status": "check-only passed; tree unchanged"}
    allowed = (Path.home() / "clearcote-ime-spike").resolve()
    if not source.is_relative_to(allowed):
        raise RuntimeError("build source must be below ~/clearcote-ime-spike")
    result["sdk"] = metadata()
    if current == pin["pristine_sha256"]:
        subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
    shutil.copyfile(helper, copied_helper)
    if digest(target) != pin["patched_sha256"]:
        raise RuntimeError("post-apply source hash mismatch")
    return {**result, "status": "adapter prepared; full-series L1/configure/build still required"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.source, args.check_only)
        status = 0
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
        status = 1
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_bytes((json.dumps(result, indent=2) + "\n").encode())
    print(json.dumps(result, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
