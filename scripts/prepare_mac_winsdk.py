#!/usr/bin/env python3
"""Complete metadata around an already-extracted, operator-provided SDK payload.

python scripts/prepare_mac_winsdk.py --toolchain ~/clearcote-ime-spike/wintoolchain \
  --json ~/clearcote-ime-spike/windows-sdk-layout.json

Only the private layout is written. This downloads nothing, installs no global
tools and performs no build. The actual Microsoft headers/libs must already be
present; their archive/manifest hash must be checked by the transfer stage.
The cl.exe file is an explicit detection marker which FAILS if invoked; it is
not a Windows compiler. The real compiler must be native mac-arm64 clang-cl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform

VC = "14.44.35207"
SDK = "10.0.26100.0"


def layout(root: Path) -> dict:
    if platform.system() != "Darwin":
        raise RuntimeError("this private SDK layout helper is for the Mac cross-build")
    root = root.expanduser().resolve(strict=True)
    if not root.is_relative_to((Path.home() / "clearcote-ime-spike").resolve()):
        raise RuntimeError("toolchain must be under ~/clearcote-ime-spike")
    vc = ["VC", "Tools", "MSVC", VC]
    sdk = ["Windows Kits", "10"]
    include = [sdk + ["Include", SDK, sub] for sub in ("um", "shared", "winrt", "cppwinrt", "ucrt")]
    include.append(vc + ["include"])
    libs = [vc + ["lib", "x64"], sdk + ["Lib", SDK, "um", "x64"], sdk + ["Lib", SDK, "ucrt", "x64"]]
    for pieces in include + libs:
        if not root.joinpath(*pieces).is_dir():
            raise RuntimeError("payload directory missing: " + "/".join(pieces))
    for pieces in (vc + ["include", "vcruntime.h"], vc + ["lib", "x64", "libcmt.lib"],
                   sdk + ["Include", SDK, "um", "windows.h"],
                   sdk + ["Lib", SDK, "um", "x64", "kernel32.lib"],
                   sdk + ["Lib", SDK, "ucrt", "x64", "ucrt.lib"]):
        if not root.joinpath(*pieces).is_file():
            raise RuntimeError("payload file missing: " + "/".join(pieces))
    if not root.joinpath(*sdk, "Include", SDK, "um", "WiNdOwS.h").is_file():
        raise RuntimeError("SDK volume is case-sensitive; use an isolated case-insensitive volume")
    bins = [vc + ["bin", "HostX64", "x64"], sdk + ["bin", SDK, "x64"]]
    env = {"VSINSTALLDIR": [["."]], "VCINSTALLDIR": [["VC"]],
           "VCToolsInstallDir": [vc], "INCLUDE": include, "LIB": libs,
           "LIBPATH": [vc + ["lib", "x64"]], "PATH": bins}
    destination = root.joinpath(*sdk, "bin", "SetEnv.x64.json")
    content = (json.dumps({"env": env}, indent=2) + "\n").encode()
    if destination.exists() and destination.read_bytes() != content:
        raise RuntimeError("refusing to overwrite unknown SetEnv.x64.json")
    link = root / "win_sdk"
    if link.is_symlink() and link.resolve() != root.joinpath(*sdk):
        raise RuntimeError("win_sdk points outside the expected SDK layout")
    if link.exists() and not link.is_symlink():
        raise RuntimeError("refusing to replace existing non-symlink win_sdk")
    for pieces in bins:
        root.joinpath(*pieces).mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    if not link.is_symlink():
        link.symlink_to("Windows Kits/10", target_is_directory=True)
    marker = root.joinpath(*bins[0], "cl.exe")
    if not marker.exists():
        marker.write_bytes(b'#!/bin/sh\necho "CLT cross-build detection marker only: use native clang-cl, not this file" >&2\nexit 1\n')
        marker.chmod(0o755)
    return {"status": "private SDK layout prepared; compiler/link/configure not tested",
            "msvc_payload_version": VC, "sdk_payload_version": SDK,
            "payload_origin": "operator-provided installed Windows VS2022/SDK files",
            "setenv_sha256": hashlib.sha256(content).hexdigest(),
            "case_insensitive_sdk_headers": True,
            "cl_exe": "detection marker only; fails if executed",
            "process_environment": {"DEPOT_TOOLS_WIN_TOOLCHAIN": "1", "GYP_MSVS_OVERRIDE_PATH": str(root),
                                    "WINDOWSSDKDIR": str(root.joinpath(*sdk)), "WDK_DIR": ""},
            "note": "Chromium150 depot-toolchain mode reports its fixed 2026 compatibility label; this is not an assertion that VS2026 was installed.",
            "remaining": "Native mac-arm64 clang/lld, Rust/Node/GN, full-series L1, actual GN configure and guarded -j1 build; add real VC runtime DLLs before packaging."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = layout(args.toolchain)
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
