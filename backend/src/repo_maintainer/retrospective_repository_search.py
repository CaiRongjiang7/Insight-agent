"""Bounded literal grep and file discovery over one server-owned snapshot.

Navigation hits are hints, not citable Evidence. Read selected ranges through the
code session to persist originals. No shell, regex execution or repository fetch.
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path, PurePosixPath


def safe_source_path(root: Path, files: set[str], path: str) -> Path:
    parts = PurePosixPath(path).parts
    if (not path or path not in files or path.startswith('/') or '\\' in path
            or ':' in path or '\x00' in path or '..' in parts):
        raise ValueError('Path is outside the indexed snapshot')
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError('Path is outside the indexed snapshot')
    return target


class SnapshotSearch:
    MAX_FILE_BYTES = 1_000_000
    MAX_SCAN_BYTES = 8_000_000
    MAX_SCAN_FILES = 1_000

    def __init__(self, root: Path, files: list[str]):
        self.root, self.files = root, set(files)

    def matching_files(self, pattern: str) -> list[str]:
        if (not pattern or pattern.startswith('/') or '\\' in pattern or ':' in pattern
                or '\x00' in pattern or '..' in PurePosixPath(pattern).parts):
            raise ValueError('Use a repository-relative file pattern')
        patterns = [pattern, pattern[3:]] if pattern.startswith('**/') else [pattern]
        return sorted(path for path in self.files if any(fnmatch.fnmatchcase(path, p) for p in patterns))

    def find_files(self, pattern: str, limit: int) -> dict:
        paths = self.matching_files(pattern)
        return {'locations': [{'file_path': p} for p in paths[:limit]],
                'truncated': len(paths) > limit,
                'search_scope': {'matching_files': len(paths), 'indexed_files': len(self.files)}}

    def search(self, query: str, pattern: str, case_sensitive: bool, limit: int, deadline: float) -> dict:
        paths = self.matching_files(pattern)
        needle = query if case_sensitive else query.casefold()
        locations, scanned, used_bytes, skipped = [], 0, 0, 0
        stopped = False
        for path in paths:
            if (scanned + skipped >= self.MAX_SCAN_FILES or used_bytes >= self.MAX_SCAN_BYTES
                    or time.monotonic() >= deadline):
                stopped = True
                break
            try:
                target = safe_source_path(self.root, self.files, path)
                with target.open('rb') as stream:
                    raw = stream.read(min(self.MAX_FILE_BYTES, self.MAX_SCAN_BYTES - used_bytes) + 1)
                used_bytes += len(raw)
                if len(raw) > self.MAX_FILE_BYTES or used_bytes > self.MAX_SCAN_BYTES or b'\x00' in raw:
                    skipped += 1
                    continue
                text = raw.decode('utf-8')
            except (OSError, UnicodeError, ValueError):
                skipped += 1
                continue
            scanned += 1
            for line_number, line in enumerate(text.splitlines(), 1):
                if time.monotonic() >= deadline:
                    stopped = True
                    break
                compared = line if case_sensitive else line.casefold()
                index = compared.find(needle)
                if index < 0:
                    continue
                if len(locations) >= limit:
                    stopped = True
                    break
                # A navigation preview is deliberately not an Evidence body.
                start = max(0, index - 60)
                locations.append({'file_path': path, 'line': line_number,
                                  'preview': line[start:start + 240]})
            if stopped:
                break
        return {'locations': locations, 'truncated': stopped or skipped > 0,
                'search_scope': {'matching_files': len(paths), 'scanned_files': scanned,
                                 'skipped_files': skipped, 'scanned_bytes': used_bytes,
                                 'scan_completed': not stopped and not skipped}}
