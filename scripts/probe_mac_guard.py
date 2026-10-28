#!/usr/bin/env python3
"""Small Mac-only guard acceptance; creates no real OS memory pressure.

Normal runs use the actual sysctl provider. Critical/warning policy probes replace
only the returned test reading after our child starts, then exercise real signal
cleanup. A separately owned sibling must survive. No global policy is changed.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import patch

import guard_mac_build as guard


def probe(root: Path):
    if sys.platform != "darwin":
        raise RuntimeError("requires actual macOS")
    root = root.expanduser().resolve()
    if not root.is_relative_to((Path.home() / "clearcote-ime-spike").resolve()):
        raise RuntimeError("probe output must stay under ~/clearcote-ime-spike")
    root.mkdir(parents=True, exist_ok=False)
    original_sample = guard.sample
    initial = original_sample(root)
    if initial["memory_pressure_level"] != 1:
        raise RuntimeError("actual OS pressure is not normal; do not run the child probes")
    reports = {}
    for name, level in (("normal", 1), ("critical", 4), ("warning", 2)):
        directory = root / name
        directory.mkdir()
        sibling = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True)
        try:
            def readings(where, pgid=None):
                result = original_sample(where, pgid)
                if pgid and level != 1:
                    result = {**result, "memory_pressure_level": level}
                return result

            child = ("import os,json;print(json.dumps({'nice':os.getpriority(os.PRIO_PROCESS,0)}),flush=True)"
                     if level == 1 else "import signal;signal.pause()")
            argv = ["guard_mac_build.py", "--directory", str(directory), "--max-hours", "0.02",
                    "--warning-grace-seconds", "1", "--", sys.executable, "-c", child]
            with patch.object(sys, "argv", argv), patch.object(guard, "sample", readings), contextlib.redirect_stdout(io.StringIO()):
                code = guard.main()
            status = json.loads((directory / "guard-status.json").read_text())
            expected = "completed" if level == 1 else (
                "memory-pressure-critical" if level == 4 else "memory-pressure-warning-persisted")
            assert status["state"] == expected, status
            assert code == (0 if level == 1 else 125), code
            assert sibling.poll() is None, "guard touched a sibling outside its owned group"
            if level == 1:
                child_result = json.loads((directory / "build.log").read_text().strip())
                assert child_result["nice"] >= 19, child_result
            group_drained = not guard.owned_group_present(status["child_pgid"])
            assert group_drained, "owned child group still exists after cleanup"
            reports[name] = {"exit_code": code, "state": status["state"], "sibling_survived": True,
                             "owned_group_drained": group_drained, "limits": status["limits"],
                             "pressure_injection": level != 1, "initial_actual_pressure": initial["memory_pressure_level"]}
        finally:
            sibling.terminate()
            sibling.wait(timeout=5)
    result = {"status": "passed", "actual_initial_readings": initial, "cases": reports}
    (root / "result.json").write_bytes((json.dumps(result, indent=2) + "\n").encode())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.directory), indent=2))
