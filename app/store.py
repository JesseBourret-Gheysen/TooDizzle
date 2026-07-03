"""Shared CSV store: cross-process locking, atomic writes, and a self-pruning
backup ring. Used by both TorrentReq and TooDizzle.

Design goals (learned the hard way — TorrentReq's requests.csv was once wiped to
all-blank rows by a non-atomic in-place truncate under only a threading.Lock):

  * Every mutation is serialized by CsvLock (threading.Lock + fcntl.flock).
  * Every mutation is written atomically (temp file + fsync + os.replace) so a
    concurrent reader never sees a half-written/empty file.
  * Every mutation snapshots the pre-mutation file into <backup_dir> first, so a
    single bad write can never destroy history.
  * Backups are deduped (a snapshot byte-identical to the newest one is skipped)
    and pruned to `record_count * 2` per store (with a small floor).
  * A disk-space floor blocks the write (InsufficientSpaceError) rather than
    letting backups fill the filesystem.

IMPORTANT: this file is kept byte-identical in both app directories
(local_torrent_requests/app/ and TooDizzle/app/). Edit one, copy to the other.
"""

import csv
import glob
import hashlib
import os
import shutil
import tempfile
import threading

# --- Tunables ---------------------------------------------------------------
MIN_FREE_BYTES = 5 * 1024 ** 3   # refuse mutations if the fs has < 5 GB free
BACKUP_CAP_FLOOR = 10            # never prune below this many backups, even if
                                 # record_count*2 would be smaller (protects the
                                 # pre-delete snapshots after a delete-to-near-empty)

# One threading.Lock per process is enough to serialize this process's threads;
# fcntl.flock in CsvLock covers the (unlikely) multi-process case.
_thread_lock = threading.Lock()


class InsufficientSpaceError(RuntimeError):
    """Raised when free disk space is below MIN_FREE_BYTES. The mutation (and its
    backup) are refused; callers should translate this to HTTP 507."""


class CsvLock:
    """Exclusive lock around every CSV mutation. Combines the module threading.Lock
    (serializes threads in this process) with an fcntl.flock on a sidecar lockfile
    (serializes across processes). Not reentrant — never nest it."""

    def __init__(self, lock_path):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        _thread_lock.acquire()
        os.makedirs(os.path.dirname(self._lock_path) or '.', exist_ok=True)
        self._fh = open(self._lock_path, 'w')
        import fcntl
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            import fcntl
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        finally:
            _thread_lock.release()


def read_rows(csv_path, fieldnames):
    """Read all rows, normalized to `fieldnames`. Caller must hold CsvLock."""
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, 'r', newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for col in fieldnames:
            row.setdefault(col, '')
    return rows


def _rows_have_data(rows, fieldnames):
    """True if any row has any non-empty field. The wipe signature is N>0 rows
    that are ALL blank; this lets us distinguish that from a real delete-to-0."""
    return any(
        any(str(r.get(col, '')).strip() for col in fieldnames)
        for r in rows
    )


def _file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _timestamped_backups(backup_dir, stem):
    """Existing auto-backups, oldest→newest. Restricted to timestamp-prefixed
    names (stem.<digit>...csv) so hand-made files like
    'requests.BLANK-preWipeRestore-*.csv' are never matched or pruned."""
    return sorted(glob.glob(os.path.join(backup_dir, stem + '.[0-9]*.csv')))


def atomic_write(csv_path, fieldnames, backup_dir, rows):
    """Atomically replace `csv_path` with `rows`. Caller must hold CsvLock.

    Order matters: space check first (so a blocked write touches nothing), then
    wipe guard, then deduped backup + prune, then the atomic replace.
    """
    from datetime import datetime

    data_dir = os.path.dirname(csv_path) or '.'

    # 1. Space check — blocks the write entirely if the disk is too full.
    if shutil.disk_usage(data_dir).free < MIN_FREE_BYTES:
        raise InsufficientSpaceError(
            'refusing to write %s: less than %d bytes free on %s'
            % (csv_path, MIN_FREE_BYTES, data_dir))

    os.makedirs(data_dir, exist_ok=True)

    if os.path.exists(csv_path):
        # 2. Wipe guard — never replace a populated store with N>0 all-blank rows.
        existing = read_rows(csv_path, fieldnames)
        if _rows_have_data(existing, fieldnames) and len(rows) > 0 \
                and not _rows_have_data(rows, fieldnames):
            raise RuntimeError(
                'refusing to overwrite %d-row store with %d all-blank rows'
                % (len(existing), len(rows)))

        # 3. Backup-on-write, deduped by content hash vs the newest backup.
        stem = os.path.splitext(os.path.basename(csv_path))[0]
        os.makedirs(backup_dir, exist_ok=True)
        existing_backups = _timestamped_backups(backup_dir, stem)
        newest = existing_backups[-1] if existing_backups else None
        if newest is None or _file_sha256(csv_path) != _file_sha256(newest):
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            try:
                shutil.copy2(csv_path, os.path.join(backup_dir, '%s.%s.csv' % (stem, stamp)))
            except Exception:
                pass

        # 4. Dynamic prune — keep at most max(record_count*2, floor) backups.
        cap = max(len(rows) * 2, BACKUP_CAP_FLOOR)
        backups = _timestamped_backups(backup_dir, stem)
        for old in backups[:-cap] if cap else backups:
            try:
                os.remove(old)
            except OSError:
                pass

    # 5. Atomic replace.
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix='.' + os.path.basename(csv_path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in fieldnames})
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, csv_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
