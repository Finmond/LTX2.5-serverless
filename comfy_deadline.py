"""Independent deadlines for one exclusively owned local ComfyUI renderer.

The guardian, not the HTTP request or asyncio loop, owns the inference session.
It remains alive after a deadline so the handler can return an error without
crashing the SDK and causing a provider retry. A dead renderer is never reused.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
import uuid

PROTOCOL = "finmond-owned-render-v1"
ROOT = Path("/tmp/finmond-render-guard")
LIMIT = 600


class GuardError(RuntimeError):
    pass


def identity(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        raise GuardError("Renderer is unavailable") from None
    fields = value[value.rfind(")") + 2:].split()
    if fields[0] == "Z":
        raise GuardError("Renderer is unavailable")
    return {"start": fields[19], "parent": int(fields[1]),
            "group": int(fields[2]), "session": int(fields[3])}


def private_read(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > 4096):
        raise GuardError("Renderer supervision is unavailable")
    return json.loads(path.read_text())


def write(path, value):
    pending = path.with_suffix(".pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def ownership(root=ROOT):
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise GuardError("Renderer supervision is unavailable")
    manifest = private_read(root / "manifest.json")
    guardian = identity(manifest["guardian"])
    renderer = identity(manifest["renderer"])
    if (manifest.get("protocol") != PROTOCOL or manifest.get("stopped") is not False
            or guardian["start"] != manifest["guardian_start"]
            or renderer["start"] != manifest["renderer_start"]
            or renderer["parent"] != manifest["guardian"]
            or renderer["group"] != manifest["renderer"]
            or renderer["session"] != manifest["renderer"]):
        raise GuardError("Renderer supervision is unavailable")
    return manifest


def arm(job_id, seconds, root=ROOT):
    if type(seconds) is not int or not 1 <= seconds <= LIMIT:
        raise GuardError("Invalid render deadline")
    manifest = ownership(root)
    path = root / "active.json"
    if path.exists():
        raise GuardError("Renderer already has an active request")
    lease = {"job": hashlib.sha256(job_id.encode()).hexdigest(),
             "deadline": time.monotonic() + seconds,
             "caller": os.getpid(), "caller_start": identity(os.getpid())["start"],
             "renderer_start": manifest["renderer_start"]}
    # Link a complete immutable lease exclusively; the guardian must never
    # observe a partial JSON write or a competing handler resetting a deadline.
    pending = root / f"active-{uuid.uuid4().hex}.json"
    write(pending, lease)
    try:
        os.link(pending, path, follow_symlinks=False)
    except FileExistsError:
        raise GuardError("Renderer already has an active request") from None
    finally:
        pending.unlink()
    return lease


def finish(lease, *, success, root=ROOT):
    current = private_read(root / "active.json")
    if current != lease:
        raise GuardError("Renderer request ownership changed")
    if not success or time.monotonic() >= lease["deadline"]:
        # Do not disarm an expired or failed inference request. The guardian
        # stops its renderer even if the handler has already returned.
        write(root / "abort.json", {"job": lease["job"]})
        if success:
            raise GuardError("Render exceeded its execution deadline")
        return
    ownership(root)
    (root / "active.json").unlink()


def stop_group(process):
    # This guardian is a Linux child subreaper and launches only the renderer.
    # Freeze its owned tree before killing it: escaped sessions and orphaned
    # grandchildren are included, and frozen parents cannot create new children.
    frozen = {}
    while True:
        rows = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                rows[int(entry.name)] = identity(int(entry.name))
            except GuardError:
                continue
        parents = {os.getpid()}
        added = True
        while added:
            added = False
            for pid, value in rows.items():
                if value["parent"] in parents and pid not in parents:
                    parents.add(pid)
                    added = True
                    if frozen.get(pid) != value["start"]:
                        try:
                            if identity(pid)["start"] == value["start"]:
                                os.kill(pid, signal.SIGSTOP)
                                frozen[pid] = value["start"]
                        except (ProcessLookupError, GuardError):
                            pass
        if all(pid == os.getpid() or pid in frozen for pid in parents):
            # Rescan after freezing; catch children forked during the snapshot.
            new_children = False
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit() or int(entry.name) in frozen:
                    continue
                try:
                    if identity(int(entry.name))["parent"] in parents:
                        new_children = True
                        break
                except GuardError:
                    pass
            if not new_children:
                break
    for pid, started in frozen.items():
        try:
            if identity(pid)["start"] == started:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, GuardError):
            pass
    process.wait(timeout=5)


def supervise(command, root=ROOT):
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise GuardError("Linux child supervision is unavailable")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    parent = os.getppid()
    process = subprocess.Popen(command, start_new_session=True)
    stopped = False

    def shutdown(*args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    manifest = {"protocol": PROTOCOL, "guardian": os.getpid(),
                "guardian_start": identity(os.getpid())["start"],
                "renderer": process.pid, "renderer_start": identity(process.pid)["start"],
                "stopped": False, "limit": LIMIT}
    write(root / "manifest.json", manifest)
    try:
        while not stopped:
            abort = process.poll() is not None or os.getppid() != parent or (root / "abort.json").exists()
            if (root / "active.json").exists():
                try:
                    lease = private_read(root / "active.json")
                    abort = (abort or time.monotonic() >= lease["deadline"]
                             or identity(lease["caller"])["start"] != lease["caller_start"]
                             or lease["renderer_start"] != manifest["renderer_start"])
                except (OSError, ValueError, KeyError, TypeError, GuardError):
                    abort = True
            if abort and not manifest["stopped"]:
                stop_group(process)
                manifest["stopped"] = True
                write(root / "manifest.json", manifest)
            time.sleep(0.05)
    finally:
        if not manifest["stopped"]:
            stop_group(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    supervise(arguments.command, arguments.root)
