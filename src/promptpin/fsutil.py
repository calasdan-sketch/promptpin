"""Small file helpers shared by the writers."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_IS_WINDOWS = os.name == "nt"


def _replace_with_retry(tmp: str, path: Path, attempts: int = 10) -> None:
    """``os.replace`` that waits briefly when Windows says the target is in use.

    On Windows, replacing a file that another process (an antivirus scan, an editor, a
    reader) has open raises ``PermissionError``; it clears within milliseconds. On POSIX
    this never retries. Not exercised on Windows (see docs/ROUND4.md).
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not _IS_WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so a reader sees the old file or the new one.

    Bytes (never text mode), so Windows does not turn ``\\n`` into ``\\r\\n``
    and a hashed file stays byte-identical on every OS. The temporary file is
    created next to the target so ``os.replace`` stays on one filesystem.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        os.chmod(tmp, mode)
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
