#!/usr/bin/env python3
"""
CLI to start/stop/restart/status the soyduel bot as a background process.

Usage:
    python3 soyduelctl.py start
    python3 soyduelctl.py stop
    python3 soyduelctl.py restart
    python3 soyduelctl.py status
    python3 soyduelctl.py logs [-n N]
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from soyduel import load_config

HERE = os.path.dirname(os.path.abspath(__file__))


def pid_from_file(pid_file):
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def cmd_start(args):
    config = load_config()
    pid_file = config["process"]["pid_file"]
    log_file = config["process"]["log_file"]

    existing = pid_from_file(pid_file)
    if existing:
        print(f"Already running (PID {existing}). Use 'restart' to apply new config.")
        return

    log_f = open(log_file, "a")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "soyduel.py")],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=HERE,
    )
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    cooldown = config["timing"]["cooldown_seconds"]
    print(f"Started (PID {proc.pid}), cooldown={cooldown}s, logging to {log_file}")


def cmd_stop(args):
    config = load_config()
    pid_file = config["process"]["pid_file"]
    pid = pid_from_file(pid_file)
    if not pid:
        print("Not running.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if pid_from_file(pid_file) is None:
            break
    else:
        print(f"PID {pid} didn't exit after 10s, sending SIGKILL.")
        os.kill(pid, signal.SIGKILL)
    if os.path.exists(pid_file):
        os.remove(pid_file)
    print(f"Stopped (was PID {pid}).")


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(1)
    load_config.cache_clear()  # reread soyduel.toml in case it changed
    cmd_start(args)


def cmd_status(args):
    config = load_config()
    pid = pid_from_file(config["process"]["pid_file"])
    if pid:
        print(f"Running (PID {pid}), cooldown={config['timing']['cooldown_seconds']}s, "
              f"thread={config['target']['board']}/{config['target']['thread_id']}")
    else:
        print("Not running.")


def cmd_watchdog(args):
    """Restart the bot if it's dead or hung. Meant to run periodically from cron."""
    config = load_config()
    pid_file = config["process"]["pid_file"]
    log_file = config["process"]["log_file"]
    now = time.time()

    pid = pid_from_file(pid_file)
    if not pid:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] watchdog: not running, starting.", flush=True)
        cmd_start(args)
        return

    # Most recent of: last log write, or last (re)start time.
    last_activity = os.path.getmtime(pid_file)
    if os.path.exists(log_file):
        last_activity = max(last_activity, os.path.getmtime(log_file))

    age = now - last_activity
    if age > args.stale_after:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] "
              f"watchdog: no activity for {age:.0f}s (PID {pid}), restarting.", flush=True)
        cmd_restart(args)


def cmd_logs(args):
    config = load_config()
    log_file = config["process"]["log_file"]
    if not os.path.exists(log_file):
        print("No log file yet.")
        return
    with open(log_file) as f:
        lines = f.readlines()
    for line in lines[-args.n:]:
        print(line, end="")


def main():
    parser = argparse.ArgumentParser(description="Control the soyduel bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    sub.add_parser("status")
    logs_parser = sub.add_parser("logs")
    logs_parser.add_argument("-n", type=int, default=20, help="number of lines to show")
    watchdog_parser = sub.add_parser("watchdog")
    watchdog_parser.add_argument("--stale-after", type=int, default=180,
                                  help="seconds of no log/start activity before considering it hung")

    args = parser.parse_args()
    {
        "start": cmd_start,
        "stop": cmd_stop,
        "watchdog": cmd_watchdog,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
    }[args.command](args)


if __name__ == "__main__":
    main()
