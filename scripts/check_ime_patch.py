#!/usr/bin/env python3
"""Cheap IME patch check against two SHA-pinned upstream files, not a full L1 gate.

Run from any directory: python scripts/check_ime_patch.py --json <report.json>
--source-dir can supply an offline pristine Chromium source root. No source tree,
toolchain, SDK or browser is installed. Full builds still require the unchanged
scripts/01-apply-patches.sh gate over the entire series.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 425, 429, 500, 502, 503, 504}
    if isinstance(error, urllib.error.URLError):
        return transient(error.reason)
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    return isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError)) or (
        isinstance(error, OSError) and error.errno in {socket.EAI_AGAIN, 101, 113, 10051, 10054, 10060, 10065})


def fetch(url: str) -> bytes:
    """At most four attempts; only transient transport/HTTP errors are retried.

    Base64 and SHA checks deliberately happen outside this retry loop.
    """
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                encoded = response.read(4 * 1024 * 1024 + 1)
            if len(encoded) > 4 * 1024 * 1024:
                raise ValueError("unexpectedly large source response")
            return encoded
        except Exception as error:
            if attempt == 3 or not transient(error):
                raise
            delay = 2 ** attempt
            print(f"transient source download failure ({type(error).__name__}); retry {attempt + 1}/3 in {delay}s", file=sys.stderr)
            time.sleep(delay)
    raise AssertionError("unreachable")


def run(source_dir: Path | None = None) -> dict:
    pin = json.loads((REPO / "scripts/ime_source_pin.json").read_text(encoding="utf-8"))
    version = (REPO / "UPSTREAM_REVISION").read_text().strip()
    if pin["chromium_version"] != version:
        raise ValueError("IME source pin does not match UPSTREAM_REVISION")
    patch = REPO / "patches" / pin["patch"]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    result = {"clearcote_commit": commit, "chromium_version": version, "patch": pin["patch"],
              "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
              "scope": "new IME patch only; full-series L1 and compiled/runtime behavior not tested",
              "sources": []}
    with tempfile.TemporaryDirectory(prefix="clearcote-ime-apply-") as temp:
        tree = Path(temp)
        for entry in pin["sources"]:
            prefix = f"https://chromium.googlesource.com/chromium/src/+/{version}/"
            if not entry["url"].startswith(prefix):
                raise ValueError("source URL is not from the pinned official Chromium version")
            relative = entry["url"][len(prefix):]
            if not relative.startswith("third_party/blink/") or ".." in Path(relative).parts:
                raise ValueError(f"unexpected source path: {relative}")
            if source_dir:
                data = (source_dir / relative).read_bytes()
            else:
                data = base64.b64decode(fetch(entry["url"] + "?format=TEXT"), validate=True)
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry["sha256"]:
                raise ValueError(f"source SHA256 mismatch: {relative}")
            destination = tree / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            result["sources"].append({"path": relative, "sha256": digest})
        for args in (["--check"], [], ["--reverse", "--check"], ["--reverse"]):
            completed = subprocess.run(["git", "apply", *args, str(patch)], cwd=tree,
                                       capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(f"git apply {' '.join(args)} failed:\n{completed.stderr}")
        for entry in result["sources"]:
            if hashlib.sha256((tree / entry["path"]).read_bytes()).hexdigest() != entry["sha256"]:
                raise AssertionError("reverse apply did not restore the pristine source")
    return {**result, "apply_and_reverse": "passed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.source_dir)
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
