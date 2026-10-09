"""
Dataclasses used across the app. Keep these dumb — no logic here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class Action(str, Enum):
    MOVE = "move"
    TRASH = "trash"
    SKIP = "skip"       # user rejected, leave in place
    IGNORE = "ignore"   # never propose this file again


class MatchReason(str, Enum):
    EXTENSION = "extension"
    NAME_REGEX = "name_regex"
    PATH_REGEX = "path_regex"
    SIZE = "size"
    MANUAL = "manual"    # user added by hand
    DUPLICATE = "duplicate"
    CONTENT = "content"  # user-defined category, matched by the local model


@dataclass(frozen=True)
class FileInfo:
    """A file we've discovered on disk."""
    path: Path
    size: int
    mtime: float
    is_cloud: bool = False       # online-only / placeholder
    is_symlink: bool = False
    drive: str = ""              # e.g. "C:" or "/" mount

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def parent(self) -> Path:
        return self.path.parent


@dataclass
class Rule:
    """A single sorting rule loaded from rules.toml."""
    name: str
    priority: int                       # lower = evaluated first
    destination: Optional[Path]         # None = "leave alone" (just categorize)
    action: Action = Action.MOVE
    extensions: set[str] = field(default_factory=set)
    name_regex: Optional[str] = None
    path_regex: Optional[str] = None    # match against full path
    min_size: Optional[int] = None
    max_size: Optional[int] = None
    exclude_path_regex: Optional[str] = None
    skip_if_project: bool = False       # for code files: skip if inside a project
    skip_if_cloud: bool = True          # always true in practice
    preserve_folders: Optional[bool] = None  # None = use config.toml default
    description: str = ""
    enabled: bool = True

    # compiled regexes are attached lazily in rules.py, not stored here


@dataclass
class MoveProposal:
    """A proposed action for a single file, shown in the UI."""
    file: FileInfo
    rule: Rule
    destination: Optional[Path]         # full path including filename
    action: Action
    reason: MatchReason
    cross_drive: bool = False
    selected: bool = True               # checkbox state in UI
    conflict: Optional[str] = None      # e.g. "name collision"


@dataclass
class MoveResult:
    """Outcome of executing a proposal."""
    proposal: MoveProposal
    success: bool
    actual_destination: Optional[Path] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)