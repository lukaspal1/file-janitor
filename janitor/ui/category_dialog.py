"""
"Sort by category" — type categories, then let the local model decide
which files belong in each.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..categories import Category, classify_files, load_categories, save_categories
from ..config import AppConfig
from ..context import assess
from ..models import Action, FileInfo, MatchReason, MoveProposal, Rule
from ..scanner import (
    _is_link,
    _should_skip_dir,
    container_dirs,
    drive_root,
    excluded_roots,
    is_cloud_placeholder,
    is_cross_drive,
    is_inside_project,
    preserved_subpath,
)

# Above this many files, warn that matching will take a while.
LARGE_SCAN_FILES = 3000


class CategoryDialog(QDialog):
    """Edit the category list. Returns Accepted when the user hits "Scan folder…"."""

    COLS = ["Category", "What belongs here (plain words)", "Destination (blank = Documents/<name>)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sort by category")
        self.resize(1000, 480)
        self.scan_root: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Type the categories you want. Describe each one in a few words — files are "
            "matched by what they're about, using a model that runs on this PC.\n"
            "Files the model isn't sure about are left where they are."
        ))

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(2, 300)
        layout.addWidget(self.table)

        # Keep fields we don't show (keywords, page limits) when saving.
        self._extra: dict[str, Category] = {}
        for c in load_categories():
            self._add_row(c)
            self._extra[c.name] = c

        buttons = QHBoxLayout()
        add = QPushButton("Add category")
        add.clicked.connect(lambda: self._add_row(None, edit=True))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        browse = QPushButton("Pick destination…")
        browse.clicked.connect(self._pick_destination)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(browse)
        buttons.addStretch(1)

        save = QPushButton("Save")
        save.clicked.connect(self._save_clicked)
        scan = QPushButton("Save && scan folder…")
        scan.setDefault(True)
        scan.clicked.connect(self._scan_clicked)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(scan)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # ---------- table ----------

    def _add_row(self, cat: Optional[Category], edit: bool = False) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(cat.name if cat else ""))
        self.table.setItem(row, 1, QTableWidgetItem(cat.description if cat else ""))
        dest = str(cat.destination) if cat and cat.destination else ""
        self.table.setItem(row, 2, QTableWidgetItem(dest))
        if edit:
            self.table.setCurrentCell(row, 0)
            self.table.editItem(self.table.item(row, 0))

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _pick_destination(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Pick destination", "Select a category row first.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Destination folder", str(Path.home() / "Documents"))
        if folder:
            self.table.item(row, 2).setText(folder)

    def categories(self) -> list[Category]:
        out, seen = [], set()
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            desc = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            dest = (self.table.item(r, 2).text() if self.table.item(r, 2) else "").strip()
            old = self._extra.get(name)
            out.append(Category(
                name=name,
                description=desc,
                destination=Path(dest).expanduser() if dest else None,
                keywords=old.keywords if old else [],
                extensions=old.extensions if old else set(),
                min_pages=old.min_pages if old else None,
                max_pages=old.max_pages if old else None,
            ))
        return out

    # ---------- buttons ----------

    def _save(self) -> bool:
        cats = self.categories()
        if len(cats) < 2:
            QMessageBox.warning(
                self, "Sort by category",
                "Add at least two categories — files are matched by comparing them.",
            )
            return False
        save_categories(cats)
        return True

    def _save_clicked(self) -> None:
        if self._save():
            QMessageBox.information(self, "Sort by category", "Categories saved.")

    def _scan_clicked(self) -> None:
        if not self._save():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Folder to sort by category", str(Path.home() / "Downloads")
        )
        if folder:
            self.scan_root = Path(folder)
            self.accept()


# ---------- background worker ----------

def collect_files(root: Path, config: AppConfig, skip_under: list[Path]) -> list[FileInfo]:
    """Same safety filters as the main scan: no system/app/hidden/cloud/project files."""
    excluded = excluded_roots(config.exclude_paths)
    skip_under = [p.resolve() for p in skip_under]
    project_cache: dict[Path, bool] = {}
    out: list[FileInfo] = []

    root = root.expanduser().resolve()
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        current = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not _should_skip_dir(current / d, excluded) and not _is_link(current / d)
        ]
        if any(current.is_relative_to(s) for s in skip_under):
            continue        # already sorted into a category
        if current not in project_cache:
            # is_inside_project looks at the file's parent; any name works.
            project_cache[current] = is_inside_project(current / "_")
        if project_cache[current]:
            continue
        drive = drive_root(current)
        for name in filenames:
            fp = current / name
            try:
                st = fp.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size < max(config.min_size, 1):
                continue
            is_cloud = is_cloud_placeholder(fp)
            if is_cloud and config.skip_cloud_sync:
                continue
            out.append(FileInfo(path=fp, size=st.st_size, mtime=st.st_mtime,
                                is_cloud=is_cloud, drive=drive))
    return out


class CategoryWorker(QThread):
    progress = pyqtSignal(str, int, int)     # stage, done, total
    finished_ok = pyqtSignal(object, int, int)  # list[MoveProposal], files examined, suggested to keep
    failed = pyqtSignal(str)

    def __init__(self, root: Path, categories: list[Category], config: AppConfig,
                 ignored: set[str]):
        super().__init__()
        self.root = root
        self.categories = categories
        self.config = config
        self.ignored = ignored

    def run(self) -> None:
        try:
            self.progress.emit("Finding files", 0, 0)
            targets = [c.target for c in self.categories]
            files = [
                f for f in collect_files(self.root, self.config, targets)
                if str(f.path) not in self.ignored
            ]
            if not files:
                self.finished_ok.emit([], 0, 0)
                return

            matches = classify_files(
                [f.path for f in files], self.categories,
                progress_cb=lambda stage, done, total: self.progress.emit(stage, done, total),
            )

            containers = container_dirs(self.root.expanduser().resolve())
            proposals: list[MoveProposal] = []
            for f, m in zip(files, matches):
                if m is None:
                    continue
                cat = m.category
                folder = cat.target.expanduser()
                if self.config.preserve_folders:
                    folder = folder / preserved_subpath(f.path.parent, containers)
                dest = folder / f.name
                cross = is_cross_drive(f.path, dest)

                how = "content" if m.from_content else "file name only"
                why = f"Matched by {how}, score {m.score:.2f}"
                if m.runner_up:
                    why += f" (next best: {m.runner_up})"
                if m.keyword_hits:
                    why += f"; keywords: {', '.join(m.keyword_hits)}"

                proposals.append(MoveProposal(
                    file=f,
                    rule=Rule(name=cat.name, priority=0, destination=cat.target,
                              action=Action.MOVE, description=why + "."),
                    destination=dest,
                    action=Action.MOVE,
                    reason=MatchReason.CONTENT,
                    cross_drive=cross,
                    selected=self.config.confirm_cross_drive is False or not cross,
                ))
            kept = assess(
                proposals, use_model=self.config.keep_check_ai,
                progress_cb=lambda stage, done, total: self.progress.emit(stage, done, total),
            )
            self.finished_ok.emit(proposals, len(files), kept)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
