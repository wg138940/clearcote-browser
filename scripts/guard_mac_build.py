#!/usr/bin/env python3
"""Run only our own process group, quietly, with a small background resource budget.

No system settings, GUI apps or other users' processes are changed. Run from an
isolated build directory; taskpolicy/nice apply only to the new child group.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

GIB = 1024 ** 3


def output(args):
    return subprocess.check_output(args, text=True, timeout=8)


def sample(directory, pgid=None):
    vm = output(['/usr/bin/vm_stat'])
    page = int(re.search(r'page size of (\d+) bytes', vm).group(1))
    counts = {k: int(v) for k, v in re.findall(r'^([^:]+):\s+(\d+)', vm, re.M)}
    reclaimable = sum(counts.get(k, 0) for k in
                      ('Pages free', 'Pages inactive', 'Pages speculative')) * page
    pressure = output(['/usr/bin/memory_pressure', '-Q'])
    pressure = int(re.search(r'free percentage:\s*(\d+)%', pressure).group(1))
    rss = 0
    if pgid:
        for row in output(['/bin/ps', '-axo', 'pgid=,rss=']).splitlines():
            group, size = row.split()
            if int(group) == pgid:
                rss += int(size) * 1024
    return dict(available_bytes=reclaimable,
                pressure_free_percent=pressure, disk_free_bytes=shutil.disk_usage(directory).free,
                group_rss_bytes=rss)


def hold_reason(readings, minimum_available, minimum_disk):
    if readings['pressure_free_percent'] < 30 or readings['available_bytes'] < minimum_available:
        return 'system-memory-low'
    if readings['disk_free_bytes'] < minimum_disk:
        return 'disk-reserve-low'
    return None


def write_status(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_bytes((json.dumps(value, indent=2) + '\n').encode())
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    parser.add_argument('--available-gib', type=float, default=6)
    parser.add_argument('--rss-budget-gib', type=float, default=8)
    parser.add_argument('--disk-reserve-gib', type=float, default=50)
    parser.add_argument('--max-hours', type=float, default=12)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('only macOS is supported')
    directory = args.directory.resolve(strict=True)
    if args.probe_only:
        readings = sample(directory)
        print(json.dumps({'readings': readings, 'hold': hold_reason(readings,
              args.available_gib * GIB, args.disk_reserve_gib * GIB)}, indent=2))
        return 0
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('a child command is required')
    lock = directory / 'guard.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        parser.error('guard.lock exists; inspect the recorded guard PID before reuse')
    status_path = directory / 'guard-status.json'
    started = time.monotonic()
    child = None
    paused = False
    halted = False
    previous = None
    exit_code = 1
    def own_signal(sig):
        # The group was created by start_new_session; never infer a group from a name.
        if child and child.poll() is None:
            if os.getpgid(child.pid) != child.pid or child.pid == os.getpgrp():
                raise RuntimeError('owned child process-group invariant failed')
            os.killpg(child.pid, sig)
    def stop_requested(*_):
        nonlocal halted
        halted = True
    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    try:
        with (directory / 'build.log').open('ab', buffering=0) as log:
            while True:
                readings = sample(directory, child.pid if child and child.poll() is None else None)
                reason = hold_reason(readings, args.available_gib * GIB, args.disk_reserve_gib * GIB)
                if halted or time.monotonic() - started > args.max_hours * 3600:
                    state = 'stopped' if halted else 'time-budget-exceeded'
                    exit_code = 124
                elif readings['group_rss_bytes'] > args.rss_budget_gib * GIB:
                    state = 'process-memory-budget-exceeded'
                    exit_code = 125
                elif child and child.poll() is not None:
                    state = 'completed' if child.returncode == 0 else 'failed'
                    exit_code = child.returncode
                else:
                    state = reason or 'running'
                    if reason and child and not paused:
                        own_signal(signal.SIGSTOP)
                        paused = True
                    elif not reason:
                        if child is None:
                            child = subprocess.Popen(['/usr/sbin/taskpolicy', '-b',
                                    '/usr/bin/nice', '-n', '19', *command], cwd=directory,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        elif paused:
                            own_signal(signal.SIGCONT)
                            paused = False
                status = dict(state=state, updated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                              guard_pid=os.getpid(), child_pgid=child.pid if child else None,
                              child_paused=paused, command=command, readings=readings,
                              limits=dict(minimum_available_gib=args.available_gib,
                                          process_rss_budget_gib=args.rss_budget_gib,
                                          disk_reserve_gib=args.disk_reserve_gib))
                write_status(status_path, status)
                if state != previous:
                    print(json.dumps(status), flush=True)
                    previous = state
                if state in ('stopped', 'time-budget-exceeded', 'process-memory-budget-exceeded', 'completed', 'failed'):
                    break
                time.sleep(2)  # bounded sampling of the observable conditions above
    finally:
        try:
            if child and child.poll() is None:
                own_signal(signal.SIGCONT)
                own_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    own_signal(signal.SIGKILL)
                    child.wait(timeout=8)
        finally:
            lock.rmdir()
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
