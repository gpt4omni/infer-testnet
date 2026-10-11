#!/usr/bin/env python3
"""Single-writer gateway storage, durable appends, and explicit testnet resets."""
from contextlib import contextmanager
import json
import os
import tempfile
import uuid


def sync_directory(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def chain_lock(path):
    """Hold across startup, any reset/recovery, and the gateway's entire life."""
    lock_path = os.path.abspath(path) + ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError(f"chain store already in use: {lock_path}") from e
        yield
    finally:
        os.close(fd)


def append_block(path, block):
    """Finish and fsync the record before any corresponding ledger commit."""
    from chain import block_record
    data = block_record(block)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    start = os.fstat(fd).st_size
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short block write")
            view = view[written:]
        os.fsync(fd)
        if start == 0:
            sync_directory(os.path.dirname(os.path.abspath(path)))
    except OSError:
        try:
            os.ftruncate(fd, start)
            os.fsync(fd)
        except OSError:
            pass  # startup can recover an unterminated final record; stop this writer
        raise
    finally:
        os.close(fd)


def repair_tail(path, offset=None):
    """Back up first. None completes a missing newline; offset drops a torn tail."""
    backup = path + ".torn-" + uuid.uuid4().hex + ".bak"
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(path, "rb") as source, os.fdopen(fd, "wb") as target:
        import shutil
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    sync_directory(os.path.dirname(os.path.abspath(path)))
    with open(path, "r+b") as f:
        if offset is None:
            f.seek(0, os.SEEK_END)
            f.write(b"\n")
        else:
            f.truncate(offset)
        f.flush()
        os.fsync(f.fileno())
    return backup


def _write_marker(path, value):
    directory = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(prefix=".reset-marker-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write((json.dumps(value, sort_keys=True) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        sync_directory(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def reset_state_once(db_path, chain_path, tag):
    """Explicit testnet reset. Preserve keys and archive chain + SQLite together."""
    if not tag or len(tag) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in tag):
        raise ValueError("reset tag must be 1..64 letters, digits, underscores or hyphens")
    db_path, chain_path = os.path.abspath(db_path), os.path.abspath(chain_path)
    marker = chain_path + ".reset-" + tag + ".done"
    if os.path.exists(marker):
        return None
    directory = os.path.dirname(chain_path)
    paths = [chain_path, db_path] + [db_path + s for s in ("-wal", "-shm", "-journal")]
    existing = [p for p in dict.fromkeys(paths) if os.path.exists(p)]
    archive, moved = None, {}
    if existing:
        archive = tempfile.mkdtemp(prefix=f"state-reset-{tag}-", dir=directory)
        for i, path in enumerate(existing):
            dest = os.path.join(archive, f"{i}-{os.path.basename(path)}")
            os.replace(path, dest)
            moved[path] = dest
            sync_directory(os.path.dirname(path))
        sync_directory(archive)
    _write_marker(marker, {"tag": tag, "archive": archive, "files": moved})
    print(f"testnet state reset: {tag}; old chain/ledger archived at {archive or '(no old state)'}; keys retained", flush=True)
    return marker
