"""
Walk the filesystem, apply rules, produce MoveProposals.

Design notes:
- We never *move* anything here. This module is read-only.
- Cloud/online-only files are detected and flagged; the rule engine
  skips them by default (skip_if_cloud=True on every rule).
- Project detection: for rules with skip_if_project=True, we walk up
  from the file's parent looking for project markers.
- Cross-drive detection: compare the drive root of the file vs. the
  destination. Cross-drive proposals are unselected by default.
- Folder preservation: a file inside a user-made subfolder keeps that
  subfolder at the destination, so clusters of related files stay together.
"""
from __future__ import annotations

import os
import stat
import fnmatch
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import (
    IS_WINDOWS,
    SKIP_DIR_NAMES,
    CLOUD_SYNC_DIR_NAMES,
    CONTAINER_DIR_NAMES,
    SYSTEM_PATHS_WINDOWS,
    SYSTEM_PATHS_MAC,
    SYSTEM_PATHS_LINUX,
    PROJECT_MARKERS,
    PROJECT_MARKER_MAX_DEPTH,
    GAME_MARKER_FILES,
    GAME_MARKER_DIRS,
    GAME_DATA_DIR_NAMES,
    GAME_HINT_DIRS,
    GAMES_DIR_NAME,
    GAME_FOLDER_STALE_DAYS,
    home_skip_paths,
)
from .models import Action, FileInfo, MatchReason, MoveProposal, Rule
from .rules import RuleSet


# ---------- drive / mount detection ----------

def drive_root(path: Path) -> str:
    """
    Return a stable identifier for the "drive" a path lives on.

    Windows:  'C:' / 'D:' / '\\\\server\\share'
    POSIX:    the mount point. We approximate by walking up until
              os.stat().st_dev changes, or until we hit '/'.
    """
    path = path.resolve()
    if IS_WINDOWS:
        anchor = path.anchor  # 'C:\\' or '\\\\server\\share\\'
        return anchor.rstrip("\\/") or anchor

    # POSIX: the destination may not exist yet — use its nearest existing ancestor.
    while not path.exists() and path != path.parent:
        path = path.parent

    # Find the highest ancestor with the same st_dev
    try:
        dev = path.stat().st_dev
    except OSError:
        return "/"
    cur = path
    while cur != cur.parent:
        try:
            if cur.parent.stat().st_dev != dev:
                break
        except OSError:
            break
        cur = cur.parent
    return str(cur)


def is_cross_drive(src: Path, dst: Path) -> bool:
    return drive_root(src) != drive_root(dst)


# ---------- cloud / placeholder detection ----------

def is_cloud_placeholder(path: Path) -> bool:
    """
    Detect OneDrive / Dropbox / iCloud / Google Drive placeholder files
    that are online-only. Moving these corrupts sync state.
    """
    # Path contains a known cloud-sync folder name?
    parts_lower = {p.lower() for p in path.parts}
    if any(name.lower() in parts_lower for name in CLOUD_SYNC_DIR_NAMES):
        return True

    # Windows: check FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (0x400000)
    # and FILE_ATTRIBUTE_OFFLINE (0x1000).
    if IS_WINDOWS:
        try:
            attrs = os.stat(path).st_file_attributes  # type: ignore[attr-defined]
            FILE_ATTRIBUTE_OFFLINE = 0x1000
            FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
            FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
            mask = (
                FILE_ATTRIBUTE_OFFLINE
                | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                | FILE_ATTRIBUTE_RECALL_ON_OPEN
            )
            if attrs & mask:
                return True
        except (OSError, AttributeError):
            pass

    # macOS: check for .icloud stub files
    if path.suffix == ".icloud" or path.name.startswith(".icloud"):
        return True

    return False


# ---------- project detection ----------

_PROJECT_MARKER_GLOBS = tuple(
    m for m in PROJECT_MARKERS if "*" in m
)
_PROJECT_MARKER_NAMES = tuple(
    m for m in PROJECT_MARKERS if "*" not in m
)


def _has_project_marker(directory: Path) -> bool:
    try:
        names = {e.name for e in os.scandir(directory)}
    except (OSError, PermissionError):
        return False

    if any(n in names for n in _PROJECT_MARKER_NAMES):
        return True
    for name in names:
        for g in _PROJECT_MARKER_GLOBS:
            if fnmatch.fnmatch(name, g):
                return True
    return False


def is_inside_project(file_path: Path, max_depth: int = PROJECT_MARKER_MAX_DEPTH) -> bool:
    """
    Walk up from the file's parent directory looking for project markers.
    Stops at max_depth or when we hit the scan root / filesystem root.
    """
    cur = file_path.parent
    for _ in range(max_depth):
        if _has_project_marker(cur):
            return True
        if cur == cur.parent:
            break
        cur = cur.parent
    return False


# ---------- game folder detection (for desktop cleanup) ----------

def looks_like_game_folder(path: Path) -> bool:
    """
    Heuristic only. We err on the side of NOT touching game folders.
    """
    try:
        entries = list(os.scandir(path))
    except (OSError, PermissionError):
        return False

    names = {e.name for e in entries}
    dirs = {e.name for e in entries if e.is_dir()}

    if dirs & GAME_MARKER_DIRS:
        return True
    for marker in GAME_MARKER_FILES:
        if "*" in marker:
            for n in names:
                if fnmatch.fnmatch(n.lower(), marker.lower()):
                    return True
        elif marker in names:
            return True
    return False


# ---------- folder preservation ----------

def container_dirs(root: Path) -> list[Path]:
    """
    Folders whose *subfolders* are treated as user-made groupings.
    Returned deepest-first so the most specific container wins.

    If the scan root is itself a user folder (e.g. ~/Desktop/cabinets),
    its parent is a container so the root's own name is preserved.
    """
    home = Path.home().resolve()
    standard = {home} | {home / n for n in CONTAINER_DIR_NAMES}
    root = root.resolve()
    containers = set(standard)
    containers.add(root if root in standard else root.parent)
    return sorted(containers, key=lambda p: len(p.parts), reverse=True)


def preserved_subpath(folder: Path, containers: list[Path]) -> Path:
    """
    The part of `folder` to recreate under a destination.

    ~/Desktop/cabinets/kitchen  ->  cabinets/kitchen
    ~/Desktop                   ->  (empty)
    """
    for c in containers:
        if folder.is_relative_to(c):
            return folder.relative_to(c)
    return Path()


# ---------- scanning ----------

@dataclass
class ScanResult:
    proposals: list[MoveProposal]
    scanned_files: int
    skipped_cloud: int
    skipped_project: int
    skipped_system: int
    errors: list[str]
    skipped_sorted: int = 0     # already inside the rule's destination


def _system_roots() -> list[Path]:
    if IS_WINDOWS:
        return [Path(p) for p in SYSTEM_PATHS_WINDOWS]
    if os.uname().sysname == "Darwin":
        return [Path(p) for p in SYSTEM_PATHS_MAC]
    return [Path(p) for p in SYSTEM_PATHS_LINUX]


def excluded_roots(extra: Optional[Iterable[Path]] = None) -> list[Path]:
    """System dirs + per-user app dirs + user excludes, resolved once."""
    roots = _system_roots() + home_skip_paths() + [Path(p).expanduser() for p in (extra or [])]
    return [r.resolve() for r in roots]


def _is_under(path: Path, roots: Iterable[Path]) -> bool:
    # `path` comes from walking a resolved root and `roots` are pre-resolved,
    # so no resolve() per directory.
    return any(path.is_relative_to(r) for r in roots)


def _is_hidden_dir(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    if IS_WINDOWS:
        FILE_ATTRIBUTE_HIDDEN = 0x2
        FILE_ATTRIBUTE_SYSTEM = 0x4
        try:
            attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    return False


def _should_skip_dir(path: Path, excluded: list[Path]) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return True
    if path.name in CLOUD_SYNC_DIR_NAMES:
        return True
    if _is_hidden_dir(path):
        return True
    if _is_under(path, excluded):
        return True
    return False


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)   # Python 3.12+
    return bool(is_junction and is_junction())


def scan(
    roots: list[Path],
    ruleset: RuleSet,
    *,
    extra_excludes: Optional[list[Path]] = None,
    skip_cloud_sync: bool = True,
    follow_symlinks: bool = False,
    min_size: int = 0,
    max_depth: int = 0,
    ignore_files: Optional[set[str]] = None,
    preserve_folders: bool = True,
    select_cross_drive: bool = False,
) -> ScanResult:
    """
    Walk each root, apply rules, and return proposals.

    max_depth=1 means "only files directly in the root".

    This function does NOT modify the filesystem.
    """
    proposals: list[MoveProposal] = []
    errors: list[str] = []
    ignored = ignore_files or set()

    excluded = excluded_roots(extra_excludes)
    rule_dests = {
        r.name: r.destination.expanduser().resolve()
        for r in ruleset.rules if r.destination is not None
    }

    scanned = skipped_cloud = skipped_project = skipped_system = skipped_sorted = 0

    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            errors.append(f"Scan root does not exist: {root}")
            continue
        containers = container_dirs(root)

        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=follow_symlinks, onerror=lambda e: errors.append(str(e))
        ):
            current = Path(dirpath)

            # Prune directories we never want to descend into.
            kept = []
            for d in dirnames:
                child = current / d
                if _should_skip_dir(child, excluded):
                    skipped_system += 1
                    continue
                if not follow_symlinks and _is_link(child):
                    continue
                kept.append(d)
            dirnames[:] = kept

            # Depth check
            if max_depth > 0:
                try:
                    depth = len(current.relative_to(root).parts)
                except ValueError:
                    depth = 0
                if depth >= max_depth:
                    dirnames[:] = []
                    continue

            current_drive = drive_root(current)
            keep_subpath = preserved_subpath(current, containers)

            for filename in filenames:
                fpath = current / filename
                try:
                    st = fpath.lstat()
                except OSError as e:
                    errors.append(f"{fpath}: {e}")
                    continue

                # Skip symlinks entirely
                if stat.S_ISLNK(st.st_mode):
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size < min_size:
                    continue
                if str(fpath) in ignored:
                    continue

                is_cloud = is_cloud_placeholder(fpath)
                if is_cloud and skip_cloud_sync:
                    skipped_cloud += 1
                    continue

                info = FileInfo(
                    path=fpath,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    is_cloud=is_cloud,
                    is_symlink=False,
                    drive=current_drive,
                )
                scanned += 1

                match = ruleset.match(info)
                if not match:
                    continue
                rule, reason = match

                # Already sorted into this rule's destination — nothing to do.
                dest_root = rule_dests.get(rule.name)
                if dest_root is not None and current.is_relative_to(dest_root):
                    skipped_sorted += 1
                    continue

                # Project check
                if rule.skip_if_project and is_inside_project(fpath):
                    skipped_project += 1
                    continue

                # Build destination
                destination: Optional[Path] = None
                action = rule.action
                if action == Action.MOVE and dest_root is not None:
                    keep = preserve_folders if rule.preserve_folders is None else rule.preserve_folders
                    folder = dest_root / keep_subpath if keep else dest_root
                    destination = folder / fpath.name
                    cross = is_cross_drive(fpath, destination)
                else:
                    cross = False

                proposals.append(
                    MoveProposal(
                        file=info,
                        rule=rule,
                        destination=destination,
                        action=action,
                        reason=reason,
                        cross_drive=cross,
                        # Cross-drive proposals unselected by default.
                        selected=(action != Action.SKIP and (select_cross_drive or not cross)),
                    )
                )

    return ScanResult(
        proposals=proposals,
        scanned_files=scanned,
        skipped_cloud=skipped_cloud,
        skipped_project=skipped_project,
        skipped_system=skipped_system,
        errors=errors,
        skipped_sorted=skipped_sorted,
    )


# ---------- Desktop folder proposals ----------
#
# When scanning the Desktop, we also want to propose moving whole folders
# (school folders, misc clutter), while skipping anything that looks like
# a game. This is separate from per-file scanning, which in Desktop mode
# only looks at loose files so folder contents aren't proposed twice.

def scan_desktop_folders(desktop: Path, ruleset: RuleSet) -> list[MoveProposal]:
    """
    Return one proposal per top-level folder on the Desktop.

    - Game-looking folders -> action=SKIP (kept in place, shown greyed out).
    - Folders that are rule destinations (e.g. Desktop/_Shortcuts) are left out.
    - Everything else -> action=MOVE as a whole folder. The suggested
      destination is <destination of the rule most of its files match>/<folder name>,
      e.g. Desktop/cabinets (mostly photos) -> Pictures/Sorted/cabinets.
      If no rule fits, destination is unset and the user picks one.
    """
    proposals: list[MoveProposal] = []
    if not desktop.exists() or not desktop.is_dir():
        return proposals

    try:
        entries = list(os.scandir(desktop))
    except (OSError, PermissionError):
        return proposals

    rule_dests = [
        r.destination.expanduser().resolve()
        for r in ruleset.rules if r.destination is not None
    ]

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        p = Path(entry.path)
        if _is_hidden_dir(p) or p.name in SKIP_DIR_NAMES:
            continue
        p_res = p.resolve()
        if any(d.is_relative_to(p_res) for d in rule_dests):
            continue
        try:
            st = p.lstat()
        except OSError:
            continue

        size, best_rule = _summarize_folder(p, ruleset)
        info = FileInfo(
            path=p,
            size=size,
            mtime=st.st_mtime,
            is_cloud=is_cloud_placeholder(p),
            is_symlink=False,
            drive=drive_root(p),
        )

        if looks_like_game_folder(p):
            proposals.append(
                MoveProposal(
                    file=info,
                    rule=Rule(
                        name="Desktop folder: game (leave alone)",
                        priority=0,
                        destination=None,
                        action=Action.SKIP,
                        description="Heuristic detected a game folder.",
                    ),
                    destination=None,
                    action=Action.SKIP,
                    reason=MatchReason.MANUAL,
                    cross_drive=False,
                    selected=False,
                )
            )
            continue

        destination = None
        cross = False
        description = "Top-level folder on Desktop."
        if best_rule is not None:
            destination = best_rule.destination.expanduser() / p.name
            cross = is_cross_drive(p, destination)
            description += f" Most of its files match '{best_rule.name}'."

        proposals.append(
            MoveProposal(
                file=info,
                rule=Rule(
                    name="Desktop folder",
                    priority=0,
                    destination=None,
                    action=Action.MOVE,
                    description=description,
                ),
                destination=destination,   # None -> UI asks the user
                action=Action.MOVE,
                reason=MatchReason.MANUAL,
                cross_drive=cross,
                selected=not cross,
            )
        )

    return proposals


# ---------- game folders in Documents ----------

_GAME_EXACT = {n.lower() for n in GAME_DATA_DIR_NAMES if not n.endswith("*")}
_GAME_PREFIX = tuple(n[:-1].lower() for n in GAME_DATA_DIR_NAMES if n.endswith("*"))
GAME_FOLDER_RULE = f"Game folders -> {GAMES_DIR_NAME}"


def _is_game_data_folder(path: Path, depth: int = 2) -> bool:
    """Known game name, or a saves/replays/… folder within `depth` levels."""
    name = path.name.lower()
    if name in _GAME_EXACT or name.startswith(_GAME_PREFIX):
        return True
    return _has_game_hint(path, depth)


def _has_game_hint(path: Path, depth: int) -> bool:
    try:
        subdirs = [e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return False
    if any(e.name.lower() in GAME_HINT_DIRS for e in subdirs):
        return True
    return depth > 1 and any(_has_game_hint(Path(e.path), depth - 1) for e in subdirs)


def _folder_stats(path: Path) -> tuple[int, float]:
    """(total size, newest modification time of anything inside)."""
    total = 0
    newest = path.stat().st_mtime
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if name in filenames:
                total += st.st_size
    return total, newest


def scan_game_folders(documents: Path) -> list[MoveProposal]:
    """
    Propose moving game data folders in Documents into Documents/My Games.

    Games write to a fixed path, so moving a folder a game still uses makes
    it start over with default settings. Folders with nothing changed in
    GAME_FOLDER_STALE_DAYS are ticked; newer ones are unticked and marked
    as suggested to keep.
    """
    proposals: list[MoveProposal] = []
    games_dir = documents / GAMES_DIR_NAME
    try:
        entries = list(os.scandir(documents))
    except OSError:
        return proposals

    cutoff = time.time() - GAME_FOLDER_STALE_DAYS * 86400
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        p = Path(entry.path)
        if p.name.lower() == GAMES_DIR_NAME.lower() or _is_hidden_dir(p):
            continue
        if is_cloud_placeholder(p) or not _is_game_data_folder(p):
            continue
        try:
            size, newest = _folder_stats(p)
        except OSError:
            continue

        last = datetime.fromtimestamp(newest).strftime("%Y-%m-%d")
        destination = games_dir / p.name
        stale = newest < cutoff
        keep_reasons = []
        if not stale:
            keep_reasons.append(
                f"Game data last changed {last}. If you still play it, the game "
                "will recreate this folder in Documents and start with default settings."
            )
        if destination.exists():
            keep_reasons.append(f"{GAMES_DIR_NAME} already has a '{p.name}' folder.")

        proposals.append(
            MoveProposal(
                file=FileInfo(path=p, size=size, mtime=newest, drive=drive_root(p)),
                rule=Rule(
                    name=GAME_FOLDER_RULE,
                    priority=0,
                    destination=games_dir,
                    action=Action.MOVE,
                    description=(
                        f"Game data folder, last changed {last}"
                        + (" - not used in over a year." if stale else ".")
                    ),
                ),
                destination=destination,
                action=Action.MOVE,
                reason=MatchReason.MANUAL,
                selected=not keep_reasons,
                keep_score=1.0 if keep_reasons else 0.0,
                keep_reasons=keep_reasons,
            )
        )
    return proposals


def _summarize_folder(path: Path, ruleset: RuleSet) -> tuple[int, Optional[Rule]]:
    """
    Walk a folder once. Return (total size, the move-rule matching the most files).
    """
    total = 0
    counts: dict[str, int] = {}
    by_name: dict[str, Rule] = {}
    is_project = _has_project_marker(path)

    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for f in filenames:
            fp = Path(dirpath) / f
            try:
                st = fp.lstat()
            except OSError:
                continue
            total += st.st_size

            match = ruleset.match(FileInfo(path=fp, size=st.st_size, mtime=st.st_mtime))
            if not match:
                continue
            rule, _ = match
            if rule.action != Action.MOVE or rule.destination is None:
                continue
            if rule.skip_if_project and is_project:
                continue
            counts[rule.name] = counts.get(rule.name, 0) + 1
            by_name[rule.name] = rule

    if not counts:
        return total, None
    # Most files wins; ties go to the higher-priority (lower number) rule.
    best = max(counts, key=lambda n: (counts[n], -by_name[n].priority))
    return total, by_name[best]
