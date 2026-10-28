#!/usr/bin/env python3
"""Real CLT SDK metadata for Mac-host Windows cross-builds; no Xcode impersonation.

Used by tools/mac-clt/host-sdk.patch only with an explicit, non-official Windows
cross-build opt-in. Missing Xcode version is reported as empty/0, never inferred
from the CLT package version. The real CLT version is recorded separately.

Light probe: taskpolicy -c utility nice -n 19 python3 mac_clt_sdk.py --probe \
  --work-dir ~/clearcote-ime-spike/clt-probe --json <report.json>
This compiles two tiny host programs and a header-free Windows COFF object;
it does not prove that the complete Chromium build graph works with CLT.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import plistlib
import re
import subprocess


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def metadata() -> dict:
    if platform.system() != "Darwin":
        raise RuntimeError("CLT host adapter requires macOS")
    developer = Path(command("xcode-select", "-print-path")).resolve()
    if developer.name != "CommandLineTools":
        raise RuntimeError("This adapter is only for the CLT provider; use upstream logic with full Xcode")
    sdk = Path(command("xcrun", "--sdk", "macosx", "--show-sdk-path")).resolve()
    clang = Path(command("xcrun", "--find", "clang")).resolve()
    package = plistlib.loads(subprocess.check_output(
        ["pkgutil", "--pkg-info-plist", "com.apple.pkg.CLTools_Executables"], timeout=30))
    version = str(package["pkg-version"])
    for name in ("ld", "libtool", "nm", "strip"):
        if not (clang.parent / name).is_file():
            raise RuntimeError(f"CLT host tool missing: {name}")
    if not (sdk / "usr/include/mach/mach_exc.defs").is_file():
        raise RuntimeError("CLT SDK is missing the Mach definitions used by host tools")
    return {"sdk_provider": "command-line-tools", "developer_dir": str(developer),
            "clt_version": version, "sdk_path": str(sdk),
            "sdk_version": command("xcrun", "--sdk", "macosx", "--show-sdk-version"),
            "sdk_build": command("xcrun", "--sdk", "macosx", "--show-sdk-build-version"),
            "bin_path": str(clang.parent), "clang_version": command(str(clang), "--version"),
            "xcode_version": "", "xcode_version_int": 0,
            "xcode_build": "CLT_" + re.sub(r"[^A-Za-z0-9_]", "_", version),
            "scope": "CLT host tools for non-official Windows cross-build only; full Xcode unavailable"}


def probe(info: dict, work: Path) -> dict:
    work = work.expanduser().resolve()
    allowed = (Path.home() / "clearcote-ime-spike").resolve()
    if not work.is_relative_to(allowed):
        raise RuntimeError("probe work directory must be under ~/clearcote-ime-spike")
    work.mkdir(parents=True, exist_ok=True)
    compiler = str(Path(info["bin_path"]) / "clang++")
    samples = {"cpp": ('#include <vector>\n#include <iostream>\nint main(){std::vector<int> v{19,23};std::cout<<v[0]+v[1];}\n', []),
               "objc": ('#import <Foundation/Foundation.h>\n#include <iostream>\nint main(){@autoreleasepool {NSString* s=@"CLT";std::cout<<s.length;}}\n', ["-framework", "Foundation"])}
    result = {}
    for name, (source, extra) in samples.items():
        path = work / (name + ".mm" if name == "objc" else name + ".cc")
        path.write_bytes(source.encode())
        binary = work / (name + "-probe")
        args = [compiler, "-std=c++20", "-isysroot", info["sdk_path"],
                "-mmacosx-version-min=12.0", str(path), "-o", str(binary), *extra]
        command(*args)
        value = command(str(binary))
        expected = "42" if name == "cpp" else "3"
        if value != expected:
            raise RuntimeError(f"{name} probe output mismatch: {value}")
        result[name] = {"command": args, "output": value, "file": command("file", str(binary))}
    source = work / "windows.cc"
    source.write_bytes(b'extern "C" int clearcote_ime_probe(){return 42;}\n')
    output = work / "windows.obj"
    command(compiler, "--target=x86_64-pc-windows-msvc", "-c", str(source), "-o", str(output))
    if output.read_bytes()[:2] != b"\x64\x86":
        raise RuntimeError("cross object is not AMD64 COFF")
    result["windows_object"] = {"file": command("file", str(output)), "scope": "header-free object only; no Windows SDK/link proof"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("value", nargs="?", default="macosx")
    parser.add_argument("--find-sdk-lines", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=Path.home() / "clearcote-ime-spike/clt-probe")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    info = metadata()
    if args.probe:
        info["probes"] = probe(info, args.work_dir)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_bytes((json.dumps(info, indent=2) + "\n").encode())
        print(json.dumps(info, indent=2))
    elif args.find_sdk_lines:
        numbers = lambda value: tuple(int(part) for part in value.split("."))
        if numbers(info["sdk_version"]) < numbers(args.value):
            raise RuntimeError("CLT SDK is below Chromium's minimum")
        print("\n".join([info["sdk_path"], info["bin_path"] + "/", info["sdk_build"], info["sdk_version"]]))
    else:
        if args.value != "macosx":
            raise RuntimeError("CLT adapter cannot build other Apple platforms")
        for name in ("xcode_version", "xcode_version_int", "xcode_build", "sdk_provider", "clt_version"):
            print(name + "=" + json.dumps(info[name]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
