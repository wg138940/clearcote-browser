#!/usr/bin/env python3
"""Run our own background process group with real macOS pressure protection.

No system settings, GUI apps or other users' processes are changed. Run from an
isolated build directory; taskpolicy utility QoS and nice19 apply only to the
new child group. Utility avoids Darwin background I/O throttling.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

GIB = 1024 ** 3


def output(args):
    return subprocess.check_output(args, text=True, timeout=8)


def sample(directory, pgid=None):
    pressure = int(output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip())
    if pressure not in (1, 2, 4):
        raise RuntimeError(f'unknown macOS memory-pressure level: {pressure}')
    rss = 0
    if pgid:
        for row in output(['/bin/ps', '-axo', 'pgid=,rss=']).splitlines():
            group, size = row.split()
            if int(group) == pgid:
                rss += int(size) * 1024
    return dict(memory_pressure_level=pressure, disk_free_bytes=shutil.disk_usage(directory).free,
                group_rss_bytes=rss)


def hold_reason(readings, minimum_disk):
    if readings['memory_pressure_level'] == 4:
        return 'memory-pressure-critical'
    if readings['disk_free_bytes'] < minimum_disk:
        return 'disk-reserve-low'
    if readings['memory_pressure_level'] == 2:
        return 'memory-pressure-warning'
    return None


def write_status(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_bytes((json.dumps(value, indent=2) + '\n').encode())
    os.replace(temp, path)


def owned_group_present(pgid):
    # Darwin can return EPERM to killpg(..., 0) after the group has drained.
    # Query only this recorded group, and never signal a different user's group.
    result = subprocess.run(['/bin/ps', '-g', str(pgid), '-o', 'pid=,pgid=,uid='],
                            capture_output=True, text=True, timeout=8)
    if result.returncode == 1 and not result.stdout.strip():
        return False
    result.check_returncode()
    members = [list(map(int, row.split())) for row in result.stdout.splitlines() if row.strip()]
    if any(group != pgid or uid != os.getuid() for _, group, uid in members):
        raise RuntimeError('owned process-group identity changed')
    return bool(members)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    parser.add_argument('--disk-reserve-gib', type=float, default=5)
    parser.add_argument('--warning-grace-seconds', type=float, default=30,
                        help='briefly pause our group on warning, then exit if it persists; never hold indefinitely')
    parser.add_argument('--max-hours', type=float, default=12)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('only macOS is supported')
    if args.disk_reserve_gib < 1 or args.warning_grace_seconds < 0:
        parser.error('disk reserve must be >=1GiB; warning grace must be nonnegative')
    directory = args.directory.resolve(strict=True)
    if args.probe_only:
        readings = sample(directory)
        print(json.dumps({'readings': readings, 'hold': hold_reason(readings,
              args.disk_reserve_gib * GIB)}, indent=2))
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
    warning_since = None
    exit_code = 1
    def own_signal(sig):
        # The group was created by start_new_session; never infer a group from a name.
        if child and owned_group_present(child.pid):
            if child.pid == os.getpgrp():
                raise RuntimeError('owned child process-group invariant failed')
            try:
                if child.poll() is None and os.getpgid(child.pid) != child.pid:
                    raise RuntimeError('owned child process-group invariant failed')
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass  # The owned group has already drained.
    def stop_requested(*_):
        nonlocal halted
        halted = True
    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    try:
        with (directory / 'build.log').open('ab', buffering=0) as log:
            while True:
                readings = sample(directory, child.pid if child and child.poll() is None else None)
                reason = hold_reason(readings, args.disk_reserve_gib * GIB)
                if reason == 'memory-pressure-warning':
                    if warning_since is None:
                        warning_since = time.monotonic()
                else:
                    warning_since = None
                if halted or time.monotonic() - started > args.max_hours * 3600:
                    state = 'stopped' if halted else 'time-budget-exceeded'
                    exit_code = 124
                elif reason in ('memory-pressure-critical', 'disk-reserve-low'):
                    state = reason
                    exit_code = 125
                elif warning_since is not None and time.monotonic() - warning_since >= args.warning_grace_seconds:
                    state = 'memory-pressure-warning-persisted'
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
                            child = subprocess.Popen(['/usr/sbin/taskpolicy', '-c', 'utility',
                                    '/usr/bin/nice', '-n', '19', *command], cwd=directory,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        elif paused:
                            own_signal(signal.SIGCONT)
                            paused = False
                status = dict(state=state, updated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                              guard_pid=os.getpid(), child_pgid=child.pid if child else None,
                              child_paused=paused, command=command, readings=readings,
                              limits=dict(memory_pressure_provider='kern.memorystatus_vm_pressure_level',
                                          warning_grace_seconds=args.warning_grace_seconds,
                                          disk_reserve_gib=args.disk_reserve_gib,
                                          rss_limit=None, user_activity_detection=False,
                                          qos='utility', requested_nice=19))
                write_status(status_path, status)
                if state != previous:
                    print(json.dumps(status), flush=True)
                    previous = state
                if state in ('stopped', 'time-budget-exceeded', 'memory-pressure-critical',
                             'memory-pressure-warning-persisted', 'disk-reserve-low', 'completed', 'failed'):
                    break
                time.sleep(2)  # bounded sampling of the observable conditions above
    finally:
        try:
            if child:
                own_signal(signal.SIGCONT)
                own_signal(signal.SIGTERM)
                deadline = time.monotonic() + 8
                while True:
                    child.poll()  # Reap the leader; descendants may still drain.
                    if not owned_group_present(child.pid):
                        break
                    if time.monotonic() >= deadline:
                        own_signal(signal.SIGKILL)
                        break
                    time.sleep(0.1)
                child.wait(timeout=8)
        finally:
            lock.rmdir()
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
