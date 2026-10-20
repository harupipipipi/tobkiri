"""Bound a reviewed isolated trial; sampled RSS is not hard containment."""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import time

GIB = 1024 ** 3


def available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise RuntimeError('missing memory availability')


def census(root):
    records = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            records[int(path.name)] = (int(fields[1]), int(fields[19]),
                                      int(fields[21]) * os.sysconf('SC_PAGE_SIZE'))
        except (FileNotFoundError, ProcessLookupError):
            continue
    included = {root} if root in records else set()
    while True:
        added = {pid for pid, (parent, _, _) in records.items()
                 if parent in included} - included
        if not added:
            break
        included.update(added)
    return {pid: records[pid] for pid in included}


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (8 * GIB, 8 * GIB))
    resource.setrlimit(resource.RLIMIT_FSIZE, (GIB, GIB))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.receipt.exists() or args.log.exists():
        raise ValueError('command and fresh receipt/log required')
    if available() < 4 * GIB or shutil.disk_usage(args.work).free < 8 * GIB:
        raise RuntimeError('preflight resource floor not met')
    started = time.monotonic()
    peak_rss = peak_count = 0
    seen = {}
    reason = None
    process = None
    try:
        with args.log.open('xb') as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, preexec_fn=limits)
            while True:
                members = census(process.pid)
                seen.update({pid: record[1] for pid, record in members.items()})
                rss = sum(record[2] for record in members.values())
                peak_rss, peak_count = max(peak_rss, rss), max(peak_count, len(members))
                if time.monotonic() - started > 1200:
                    raise RuntimeError('wall timeout')
                if rss > 3 * GIB or len(members) > 64:
                    raise RuntimeError('sampled resource guard')
                if available() < GIB or shutil.disk_usage(args.work).free < 8 * GIB:
                    raise RuntimeError('host resource floor')
                exited = os.waitid(os.P_PID, process.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    exit_code = (exited.si_status if exited.si_code == os.CLD_EXITED
                                 else -exited.si_status)
                    break
                time.sleep(0.1)
            if exit_code != 0:
                raise RuntimeError(f'trial exit {exit_code}')
    except BaseException as error:
        reason = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        if process is not None:
            # Kill the private namespace monitor/group, never unrelated processes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                reason = reason or 'cleanup wait timeout'
            remaining = []
            for _ in range(50):
                remaining = []
                for pid, start in seen.items():
                    try:
                        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                        if int(fields[19]) == start and fields[0] != 'Z':
                            remaining.append(pid)
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                if not remaining:
                    break
                time.sleep(0.1)
            record = {'outcome': 'success' if reason is None and not remaining else 'failed',
                      'error': reason, 'exit_code': process.returncode,
                      'seconds': time.monotonic() - started,
                      'observed_peak_sampled_rss': peak_rss,
                      'observed_max_processes': peak_count,
                      'remaining_known_processes': remaining,
                      'address_space_limit_per_process': 8 * GIB,
                      'sampled_rss_limit': 3 * GIB, 'sampled_process_limit': 64,
                      'timeout_seconds': 1200, 'sample_interval_seconds': 0.1,
                      'rss_and_process_sampling_are_not_hard_caps': True,
                      'disk_free_floor': 8 * GIB,
                      'initial_available_memory_floor': 4 * GIB,
                      'running_available_memory_floor': GIB}
            args.receipt.write_text(json.dumps(record, indent=2) + '\n')
            if record['outcome'] != 'success':
                raise RuntimeError(reason or 'sampled trial descendants remain')


if __name__ == '__main__':
    main()
