"""Measure encrypted storage without reading contents or deleting files."""
import os
import pathlib
import stat

def bytes_in(path, logical=False):
    total = 0
    seen = set()
    def fail(error): raise error
    if not path.exists(): return 0
    for root, dirs, files in os.walk(path, followlinks=False, onerror=fail):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += st.st_size if logical else st.st_blocks * 512
    return total

def usage(root, limit, incoming=0):
    root = pathlib.Path(root)
    live = bytes_in(root / "files", logical=True)
    history = bytes_in(root / "git") + bytes_in(root / "recovery")
    fs = os.statvfs(root)
    free = fs.f_bavail * fs.f_frsize
    capacity = fs.f_blocks * fs.f_frsize
    history_limit = limit * 18 // 10
    reserve = max(64 * 1024 * 1024, min(capacity // 10, limit // 10))
    projected = incoming * 2 + (1024 * 1024 if incoming else 0)
    reasons = []
    if live >= limit: reasons.append("live-limit")
    if history + projected >= history_limit: reasons.append("history-limit")
    if free - projected <= reserve: reasons.append("disk-reserve")
    live_fs = os.statvfs(root / "files")
    if live_fs.f_bavail * live_fs.f_frsize <= 0: reasons.append("live-volume-full")
    return dict(liveBytes=live, historyBytes=history, quotaBytes=limit,
        historyBudgetBytes=history_limit, freeBytes=free,
        reserveBytes=reserve, blocked=bool(reasons), reasons=reasons)
