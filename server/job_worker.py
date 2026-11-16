"""Independent shell supervisor. Only this process signals its owned process group."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import signal
import sqlite3
import subprocess
import time


def _connect(database):
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _leader_exited(process):
    # Keep the leader unreaped while killing the group, preventing PID reuse.
    information = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return information is not None


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM when a group consists only of zombies.
        if _live_group_members(process.pid):
            raise


def _live_group_members(group_id):
    probe = subprocess.run(["/bin/ps", "-e", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, check=True)
    members = []
    for line in probe.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise RuntimeError("Unexpected process group inspection output")
        if int(fields[1]) == group_id and not fields[2].startswith("Z"):
            members.append(int(fields[0]))
    return members


def _finish_group(process, *, grace=0.3):
    if _leader_exited(process) and not _live_group_members(process.pid):
        return process.wait(timeout=10)
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while _live_group_members(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _live_group_members(process.pid):
        _signal_group(process, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while _live_group_members(process.pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("Command process group cleanup could not be confirmed")
        time.sleep(0.05)
    return process.wait(timeout=10)


def run(database: Path, job_id: str):
    connection = _connect(database)
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] != "queued":
        connection.rollback()
        connection.close()
        return
    now = time.time()
    tenant = connection.execute("SELECT * FROM tenants WHERE id = ?", (row["tenant_id"],)).fetchone()
    if row["cancel_requested"]:
        connection.execute("UPDATE jobs SET status = 'cancelled', ended_at = ?, updated_at = ? WHERE id = ?",
                           (now, now, job_id))
        connection.commit()
        connection.close()
        return
    connection.execute("UPDATE jobs SET status = 'running', worker_pid = ?, started_at = ?, heartbeat = ?, "
                       "updated_at = ?, error = NULL WHERE id = ?", (os.getpid(), now, now, now, job_id))
    connection.commit()
    directory = database.parent / "jobs" / job_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = None
    interrupted = False

    def request_stop(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    status, error, exit_code = "failed", None, None
    try:
        if os.name != "posix" or not hasattr(os, "WNOWAIT"):
            raise RuntimeError("Remote shell supervision requires POSIX waitid/WNOWAIT")
        identity = {}
        environment = dict(os.environ)
        if tenant["execution_user"]:
            if os.geteuid() != 0:
                raise RuntimeError("Configured execution_user requires a root service supervisor")
            account = pwd.getpwnam(tenant["execution_user"])
            if account.pw_uid == 0 or account.pw_uid != tenant["execution_uid"] or account.pw_gid != tenant["execution_gid"]:
                raise RuntimeError("Configured execution user identity changed")
            identity = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
            environment.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
            environment["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
            environment["TMPDIR"] = "/tmp"
        with (directory / "stdout.log").open("ab", buffering=0) as stdout, (directory / "stderr.log").open("ab", buffering=0) as stderr:
            process = subprocess.Popen(["/bin/sh", "-c", row["command"]], cwd=row["cwd"], env=environment,
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True, close_fds=True, **identity)
            connection.execute("UPDATE jobs SET command_pid = ?, updated_at = ? WHERE id = ?",
                               (process.pid, time.time(), job_id))
            deadline = time.monotonic() + row["timeout_seconds"]
            heartbeat_at = 0.0
            while True:
                state = connection.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if interrupted or state["cancel_requested"]:
                    status = "cancelled"
                    exit_code = _finish_group(process)
                    break
                if _leader_exited(process):
                    exit_code = _finish_group(process)
                    status = "completed" if exit_code == 0 else "failed"
                    break
                if time.monotonic() >= deadline:
                    error = f"Command exceeded timeout_seconds={row['timeout_seconds']:g}"
                    exit_code = _finish_group(process)
                    status = "failed"
                    break
                if time.monotonic() >= heartbeat_at:
                    connection.execute("UPDATE jobs SET heartbeat = ?, updated_at = ? WHERE id = ?",
                                       (time.time(), time.time(), job_id))
                    heartbeat_at = time.monotonic() + 1
                time.sleep(0.1)
    except BaseException as exception:
        error = f"{type(exception).__name__}: {exception}"
        if process is not None and process.returncode is None:
            try:
                exit_code = _finish_group(process)
            except BaseException as cleanup_error:
                error += f"; process cleanup failed: {cleanup_error}"
    finally:
        now = time.time()
        connection.execute("UPDATE jobs SET status = ?, exit_code = ?, error = ?, ended_at = ?, updated_at = ?, "
                           "heartbeat = ? WHERE id = ?", (status, exit_code, error, now, now, now, job_id))
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run(args.database.resolve(), args.job)


if __name__ == "__main__":
    main()
