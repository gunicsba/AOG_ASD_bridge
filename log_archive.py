"""
Log housekeeping for the bridge.

Every run writes its own log to logs/. At startup the logs of earlier runs
are zipped into logs/archive/<YYYY-MM-DD>.zip (one archive per day, the
DEBUG logs compress ~10x) and archives older than keep_days are deleted. export_logs() bundles recent logs into one zip for sending.
"""

import glob
import os
import re
import time
import zipfile

ARCHIVE_SUBDIR = "archive"
_STAMP = re.compile(r"_(\d{8})_(\d{6})\.log$")


def _run_time(name: str, fallback: float) -> float:
    """Start time of a run from its log name (..._YYYYmmdd_HHMMSS.log)."""
    m = _STAMP.search(name)
    if m:
        try:
            return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
        except ValueError:
            pass
    return fallback


def _day_start(t: float) -> float:
    return time.mktime(time.strptime(time.strftime("%Y%m%d", time.localtime(t)), "%Y%m%d"))


def _archive_day(path: str) -> float:
    """Day of a logs/archive/<YYYY-MM-DD>.zip (older monthly ones by mtime)."""
    try:
        return time.mktime(time.strptime(os.path.basename(path)[:10], "%Y-%m-%d"))
    except ValueError:
        return os.path.getmtime(path)


def archive_old_logs(log_dir: str, current: str, keep_days: int,
                     legacy_dir: str = "", legacy_prefixes=()):
    """Zip every finished log in log_dir (and old-style logs in legacy_dir
    matching legacy_prefixes) into the monthly archive. Returns
    (archived, pruned) counts. keep_days <= 0 keeps archives forever."""
    archive_dir = os.path.join(log_dir, ARCHIVE_SUBDIR)
    current = os.path.normcase(os.path.abspath(current))

    candidates = glob.glob(os.path.join(log_dir, "*.log"))
    candidates += glob.glob(os.path.join(log_dir, "*.log.archiving"))
    if legacy_dir:
        for prefix in legacy_prefixes:
            candidates += glob.glob(os.path.join(legacy_dir, prefix + "*.log"))

    archived = 0
    for path in sorted(set(candidates)):
        if os.path.normcase(os.path.abspath(path)) == current:
            continue
        name = os.path.basename(path)
        if name.endswith(".archiving"):
            name = name[:-len(".archiving")]
            busy = path
        else:
            # Renaming fails while another process still writes the file
            busy = os.path.join(log_dir, name + ".archiving")
            try:
                os.replace(path, busy)
            except OSError:
                continue
        day = time.strftime("%Y-%m-%d", time.localtime(
            _run_time(name, os.path.getmtime(busy))))
        os.makedirs(archive_dir, exist_ok=True)
        with zipfile.ZipFile(os.path.join(archive_dir, f"{day}.zip"), "a",
                             zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            if name not in z.namelist():
                z.write(busy, arcname=name)
        os.remove(busy)
        archived += 1

    pruned = 0
    if keep_days > 0:
        cutoff = _day_start(time.time() - keep_days * 86400)
        for path in glob.glob(os.path.join(archive_dir, "*.zip")):
            if _archive_day(path) < cutoff:
                try:
                    os.remove(path)
                    pruned += 1
                except OSError:
                    pass
    return archived, pruned


def export_logs(dest: str, log_dir: str, days: int, extra_files=()) -> int:
    """Write the logs of the last `days` days (loose and archived) plus
    extra_files (e.g. config.ini) into the zip `dest`. Returns the number
    of logs included."""
    cutoff = time.time() - days * 86400
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for path in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
            if _run_time(os.path.basename(path), os.path.getmtime(path)) >= cutoff \
                    or os.path.getmtime(path) >= cutoff:
                out.write(path, arcname=os.path.basename(path))
                count += 1
        archives = sorted(glob.glob(os.path.join(log_dir, ARCHIVE_SUBDIR, "*.zip")))
        for arc in archives:
            if _archive_day(arc) < _day_start(cutoff):
                continue
            with zipfile.ZipFile(arc) as z:
                for info in z.infolist():
                    if _run_time(info.filename, cutoff) >= cutoff \
                            and info.filename not in out.namelist():
                        out.writestr(info, z.read(info))
                        count += 1
        for path in extra_files:
            if os.path.exists(path):
                out.write(path, arcname=os.path.basename(path))
    return count
