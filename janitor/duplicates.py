"""
Duplicate file detection.

Three-stage algorithm:
  1. Group by exact size.
  2. For size-ties, compare partial hashes (first + last 8 KB).
  3. For partial-hash ties, full SHA-256.

Returns DuplicateGroups that the UI can present for user review.
Nothing here deletes anything.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import FileInfo


# How many bytes to read from the start and end of each file for the
# partial-hash stage. 8 KB is the sweet spot: fast enough to be cheap,
# large enough to kill most false positives.
PARTIAL_CHUNK = 8 * 1024

# Hash read chunk size for full hashing.
READ_CHUNK = 1024 * 1024  # 1 MB


@dataclass
class DuplicateGroup:
    """A set of files with identical content."""
    size: int
    digest: str                  # full SHA-256, hex
    files: list[FileInfo] = field(default_factory=list)
    # The file the tool recommends keeping. User can override in the UI.
    keeper: Optional[FileInfo] = None

    @property
    def wasted_bytes(self) -> int:
        """Bytes that would be freed by trashing everything except the keeper."""
        return self.size * (len(self.files) - 1)

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass
class DuplicateScanResult:
    groups: list[DuplicateGroup]
    files_examined: int
    partial_hash_hits: int
    full_hash_hits: int
    errors: list[str] = field(default_factory=list)

    @property
    def total_wasted(self) -> int:
        return sum(g.wasted_bytes for g in self.groups)


# ---------- hashing ----------

def _partial_hash(path: Path, size: int) -> str:
    """
    Hash first + last PARTIAL_CHUNK bytes. For files <= 2*PARTIAL_CHUNK,
    just hash the whole thing (it's small anyway).
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            if size <= 2 * PARTIAL_CHUNK:
                while chunk := f.read(READ_CHUNK):
                    h.update(chunk)
            else:
                h.update(f.read(PARTIAL_CHUNK))
                f.seek(-PARTIAL_CHUNK, os.SEEK_END)
                h.update(f.read(PARTIAL_CHUNK))
    except OSError as e:
        raise RuntimeError(f"partial hash failed: {e}") from e
    return h.hexdigest()


def _full_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(READ_CHUNK):
                h.update(chunk)
    except OSError as e:
        raise RuntimeError(f"full hash failed: {e}") from e
    return h.hexdigest()


# ---------- inode / hardlink helpers ----------

def _identity(path: Path) -> tuple[int, int] | None:
    """
    Return (st_dev, st_ino) — the OS's identity for the underlying file.
    Two paths with the same identity are hardlinks to the same data,
    not duplicates. Returns None if stat fails or if st_ino is 0
    (some Windows filesystems).
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_ino == 0:
        return None
    return (st.st_dev, st.st_ino)


# ---------- keeper heuristic ----------

def choose_keeper(files: list[FileInfo]) -> FileInfo:
    """
    Default keeper:
      1. Prefer the shallowest path (shortest depth).
      2. Tie-break by oldest mtime (the "original").
      3. Tie-break by shortest path string (deterministic).
    """
    def key(f: FileInfo):
        depth = len(f.path.parts)
        return (depth, f.mtime, len(str(f.path)))

    return min(files, key=key)


# ---------- main entry ----------

def find_duplicates(
    files: Iterable[FileInfo],
    *,
    min_size: int = 1,          # skip empty files by default
    skip_hardlinks: bool = True,
    progress_cb=None,           # optional callable(stage: str, done: int, total: int)
) -> DuplicateScanResult:
    """
    Find groups of byte-identical files.

    `files` should already be filtered by the scanner (system dirs excluded,
    cloud placeholders excluded, etc.).
    """
    errors: list[str] = []

    # ---- filter ----
    candidates = [
        f for f in files
        if f.size >= min_size and not f.is_cloud
    ]

    # ---- stage 0: drop hardlinks that point to the same inode ----
    if skip_hardlinks:
        seen_inodes: dict[tuple[int, int], FileInfo] = {}
        deduped: list[FileInfo] = []
        for f in candidates:
            ident = _identity(f.path)
            if ident is None:
                deduped.append(f)
                continue
            if ident in seen_inodes:
                # Same inode as an earlier file — not a duplicate.
                continue
            seen_inodes[ident] = f
            deduped.append(f)
        candidates = deduped

    # ---- stage 1: group by size ----
    by_size: dict[int, list[FileInfo]] = {}
    for f in candidates:
        by_size.setdefault(f.size, []).append(f)

    size_groups = [g for g in by_size.values() if len(g) > 1]

    # ---- stage 2: partial hash ----
    by_partial: dict[tuple[int, str], list[FileInfo]] = {}
    partial_hits = 0
    for group in size_groups:
        for f in group:
            try:
                ph = _partial_hash(f.path, f.size)
            except RuntimeError as e:
                errors.append(f"{f.path}: {e}")
                continue
            by_partial.setdefault((f.size, ph), []).append(f)

    partial_groups = [g for g in by_partial.values() if len(g) > 1]
    partial_hits = sum(len(g) for g in partial_groups)

    # ---- stage 3: full hash ----
    by_full: dict[tuple[int, str], list[FileInfo]] = {}
    full_hits = 0
    for group in partial_groups:
        for f in group:
            try:
                fh = _full_hash(f.path)
            except RuntimeError as e:
                errors.append(f"{f.path}: {e}")
                continue
            by_full.setdefault((f.size, fh), []).append(f)

    full_groups = {k: g for k, g in by_full.items() if len(g) > 1}
    full_hits = sum(len(g) for g in full_groups.values())

    # ---- build result ----
    result_groups: list[DuplicateGroup] = []
    for (size, digest), files in full_groups.items():
        files_sorted = sorted(files, key=lambda f: str(f.path))
        group = DuplicateGroup(
            size=size,
            digest=digest,
            files=files_sorted,
            keeper=choose_keeper(files_sorted),
        )
        result_groups.append(group)

    # Sort groups by wasted space descending — biggest wins first
    result_groups.sort(key=lambda g: g.wasted_bytes, reverse=True)

    return DuplicateScanResult(
        groups=result_groups,
        files_examined=len(candidates),
        partial_hash_hits=partial_hits,
        full_hash_hits=full_hits,
        errors=errors,
    )