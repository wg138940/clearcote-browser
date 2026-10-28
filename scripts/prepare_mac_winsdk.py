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


def layout(root: Path, architectures=("x64",)) -> dict:
    if platform.system() != "Darwin":
        raise RuntimeError("this private SDK layout helper is for the Mac cross-build")
    root = root.expanduser().resolve(strict=True)
    if not root.is_relative_to((Path.home() / "clearcote-ime-spike").resolve()):
        raise RuntimeError("toolchain must be under ~/clearcote-ime-spike")
    vc = ["VC", "Tools", "MSVC", VC]
    sdk = ["Windows Kits", "10"]
    include = [sdk + ["Include", SDK, sub] for sub in ("um", "shared", "winrt", "cppwinrt", "ucrt")]
    include.append(vc + ["include"])
    include.append(vc + ["atlmfc", "include"])
    if not architectures or any(arch not in ("x64", "x86", "arm64") for arch in architectures):
        raise ValueError("architectures must explicitly name x64, x86 or arm64")
    outputs = {}
    for arch in architectures:
        libs = [vc + ["lib", arch], vc + ["atlmfc", "lib", arch], sdk + ["Lib", SDK, "um", arch], sdk + ["Lib", SDK, "ucrt", arch]]
        for pieces in include + libs:
            if not root.joinpath(*pieces).is_dir():
                raise RuntimeError("payload directory missing: " + "/".join(pieces))
        for pieces in (vc + ["include", "vcruntime.h"], vc + ["lib", arch, "libcmt.lib"],
                       vc + ["atlmfc", "include", "atldef.h"], vc + ["atlmfc", "lib", arch, "atls.lib"],
                       sdk + ["Include", SDK, "um", "windows.h"],
                       sdk + ["Lib", SDK, "um", arch, "kernel32.lib"],
                       sdk + ["Lib", SDK, "ucrt", arch, "ucrt.lib"]):
            if not root.joinpath(*pieces).is_file():
                raise RuntimeError("payload file missing: " + "/".join(pieces))
        bins = [vc + ["bin", "HostX64", arch], sdk + ["bin", SDK, arch]]
        env = {"VSINSTALLDIR": [["."]], "VCINSTALLDIR": [["VC"]],
               "VCToolsInstallDir": [vc], "INCLUDE": include, "LIB": libs,
               "LIBPATH": [vc + ["lib", arch]], "PATH": bins}
        destination = root.joinpath(*sdk, "bin", f"SetEnv.{arch}.json")
        content = (json.dumps({"env": env}, indent=2) + "\n").encode()
        if destination.exists() and destination.read_bytes() != content:
            raise RuntimeError(f"refusing to overwrite unknown SetEnv.{arch}.json")
        outputs[arch] = (destination, content, bins)
    if not root.joinpath(*sdk, "Include", SDK, "um", "WiNdOwS.h").is_file():
        raise RuntimeError("SDK volume is case-sensitive; use an isolated case-insensitive volume")
    link = root / "win_sdk"
    if link.is_symlink() and link.resolve() != root.joinpath(*sdk):
        raise RuntimeError("win_sdk points outside the expected SDK layout")
    if link.exists() and not link.is_symlink():
        raise RuntimeError("refusing to replace existing non-symlink win_sdk")
    for destination, content, bins in outputs.values():
        for pieces in bins:
            root.joinpath(*pieces).mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        marker = root.joinpath(*bins[0], "cl.exe")
        if not marker.exists():
            marker.write_bytes(b'#!/bin/sh\necho "CLT cross-build detection marker only: use native clang-cl, not this file" >&2\nexit 1\n')
            marker.chmod(0o755)
    if not link.is_symlink():
        link.symlink_to("Windows Kits/10", target_is_directory=True)
    return {"status": "private SDK layout prepared; compiler/link/configure not tested",
            "msvc_payload_version": VC, "sdk_payload_version": SDK,
            "payload_origin": "operator-provided installed Windows VS2022/SDK files",
            "setenv_sha256": {arch: hashlib.sha256(value[1]).hexdigest() for arch, value in outputs.items()},
            "architectures": list(architectures),
            "case_insensitive_sdk_headers": True,
            "cl_exe": "detection marker only; fails if executed",
            "process_environment": {"DEPOT_TOOLS_WIN_TOOLCHAIN": "1", "GYP_MSVS_OVERRIDE_PATH": str(root),
                                    "WINDOWSSDKDIR": str(root.joinpath(*sdk)), "WDK_DIR": ""},
            "note": "Chromium150 depot-toolchain mode reports its fixed 2026 compatibility label; this is not an assertion that VS2026 was installed.",
            "remaining": "Native mac-arm64 clang/lld, Rust/Node/GN, full-series L1, actual GN configure and guarded build; add real VC runtime DLLs before packaging."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--architectures", nargs="+", choices=("x64", "x86", "arm64"), default=["x64"])
    args = parser.parse_args()
    try:
        result = layout(args.toolchain, args.architectures)
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
