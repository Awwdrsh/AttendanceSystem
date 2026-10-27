"""Append-only raw payload archive (plan §8.2). Port of apps/gateway/src/archive.ts.

data/raw/<SN>/<YYYY-MM-DD>[.n].log: one file per device per day; when a day file passes 50 MB the suffix increments,
up to MAX_FILES_PER_DAY. Bodies are written verbatim; nothing else logs them.
"""
from __future__ import annotations

import os
import re
import time

from .jsutil import iso_ms

MAX_FILE_BYTES = 50 * 1024 * 1024
#: At most this many 50 MB files per device per day; beyond it bodies are dropped (disk-fill protection).
MAX_FILES_PER_DAY = 5


def safe_serial(sn: str) -> str:
    """Serial numbers are used as directory names: only [A-Za-z0-9_-], max 64 chars, never '.' sequences."""
    s = re.sub(r'[^A-Za-z0-9_-]', '_', sn)[:64]
    if re.fullmatch(r'_+', s):
        s = ''
    return s or 'unknown'


class RawArchive:
    def __init__(self, dir: str | None):
        self.dir = dir
        self.dropped = 0

    def append(self, sn: str, kind: str, body: str | bytes, now_ms: int | None = None) -> str | None:
        if not self.dir:
            return None
        now = int(time.time() * 1000) if now_ms is None else now_ms
        safe_sn = safe_serial(sn)
        day = iso_ms(now)[:10]
        folder = os.path.join(self.dir, safe_sn)
        if not os.path.abspath(folder).startswith(os.path.abspath(self.dir) + os.sep):
            return None
        data = body.encode('utf-8') if isinstance(body, str) else bytes(body)
        try:
            os.makedirs(folder, exist_ok=True)
            n = 0
            file = os.path.join(folder, f'{day}.log')
            while os.path.exists(file) and os.stat(file).st_size >= MAX_FILE_BYTES:
                n += 1
                if n >= MAX_FILES_PER_DAY:
                    self.dropped += 1
                    return None
                file = os.path.join(folder, f'{day}.{n}.log')
            header = f'--- {iso_ms(now)} {re.sub(r"[\r\n]", " ", kind)} {len(data)}B\n'.encode('utf-8')
            with open(file, 'ab') as f:
                f.write(header + data + b'\n')
            return file
        except OSError:
            self.dropped += 1
            return None
