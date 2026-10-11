"""
Main window. Three-pane layout:

  [Groups]  [Proposal table]  [Preview / actions]

Also hosts the duplicate finder, retry queue, undo dialog, and
Desktop-cleanup mode.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import desktop_icons
from ..config import DEFAULT_RULES_PATH, IS_WINDOWS, AppConfig, load_config
from ..context import assess
from ..db import JanitorDB
from ..duplicates import find_duplicates
from ..models import (
    Action,
    FileInfo,
    MatchReason,
    MoveProposal,
    MoveResult,
    Rule,
)
from ..mover import execute_batch, undo_move
from ..rules import load_rules
from ..scanner import (
    _is_link,
    _should_skip_dir,
    drive_root,
    excluded_roots,
    is_cloud_placeholder,
    is_cross_drive,
    scan,
    scan_desktop_folders,
    scan_game_folders,
)
from .category_dialog import LARGE_SCAN_FILES, CategoryDialog, CategoryWorker
from .widgets import KEEP_GROUP, GroupList, PreviewPane, ProposalTable, human_size, suggests_keep


# ---------- background scan worker ----------

class ScanWorker(QThread):
    progress = pyqtSignal(str, int, int)     # stage, done, total
    finished_ok = pyqtSignal(object, int)    # ScanResult, files suggested to keep
    failed = pyqtSignal(str)

    def __init__(self, roots, ruleset, desktop_mode: bool, ignored: set[str], config: AppConfig):
        super().__init__()
        self.roots = roots
        self.ruleset = ruleset
        self.desktop_mode = desktop_mode
        self.ignored = ignored
        self.config = config

    def run(self) -> None:
        try:
            proposals: list[MoveProposal] = []

            if self.desktop_mode:
                desktop = Path.home() / "Desktop"
                proposals.extend(scan_desktop_folders(desktop, self.ruleset))

            cfg = self.config
            result = scan(
                roots=self.roots,
                ruleset=self.ruleset,
                ignore_files=self.ignored,
                extra_excludes=cfg.exclude_paths,
                skip_cloud_sync=cfg.skip_cloud_sync,
                follow_symlinks=cfg.follow_symlinks,
                min_size=cfg.min_size,
                # Desktop mode moves top-level folders whole, so only look at
                # loose files here — otherwise folder contents get proposed twice.
                max_depth=1 if self.desktop_mode else cfg.max_depth,
                preserve_folders=cfg.preserve_folders,
                select_cross_drive=not cfg.confirm_cross_drive,
            )
            proposals.extend(result.proposals)

            # Scanning Documents (directly or via home): offer to gather game
            # folders into My Games, and drop file proposals inside them.
            documents = (Path.home() / "Documents").resolve()
            if not self.desktop_mode and any(
                documents.is_relative_to(r.expanduser().resolve()) for r in self.roots
            ):
                game_folders = scan_game_folders(documents)
                inside = [g.file.path.resolve() for g in game_folders]
                proposals = [
                    p for p in proposals
                    if not any(p.file.path.is_relative_to(g) for g in inside)
                ] + game_folders

            result.proposals = proposals
            kept = assess(
                proposals, use_model=cfg.keep_check_ai,
                progress_cb=lambda stage, done, total: self.progress.emit(stage, done, total),
            )
            self.finished_ok.emit(result, kept)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# ---------- background duplicate worker ----------

class DuplicateWorker(QThread):
    finished_ok = pyqtSignal(object)   # DuplicateScanResult
    failed = pyqtSignal(str)

    def __init__(self, roots: list[Path], config: AppConfig):
        super().__init__()
        self.roots = roots
        self.config = config

    def run(self) -> None:
        try:
            all_files: list[FileInfo] = []
            excluded = excluded_roots(self.config.exclude_paths)

            for root in self.roots:
                root = root.expanduser().resolve()
                if not root.exists():
                    continue
                for dirpath, dirnames, filenames in os.walk(
                    root, onerror=lambda e: None
                ):
                    current = Path(dirpath)
                    dirnames[:] = [
                        d for d in dirnames
                        if not _should_skip_dir(current / d, excluded)
                        and not _is_link(current / d)
                    ]
                    for name in filenames:
                        fp = current / name
                        try:
                            st = fp.lstat()
                        except OSError:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        all_files.append(
                            FileInfo(
                                path=fp,
                                size=st.st_size,
                                mtime=st.st_mtime,
                                is_cloud=is_cloud_placeholder(fp),
                                drive=drive_root(fp),
                            )
                        )

            result = find_duplicates(all_files)
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("File Janitor")
        self.resize(1400, 850)

        self.db = JanitorDB()
        self.config = load_config()
        self.ruleset = load_rules(DEFAULT_RULES_PATH)
        self.proposals: list[MoveProposal] = []
        self.current_group: str = ""
        self.retry_queue: list[MoveProposal] = []
        self.desktop_mode = False
        self.scan_roots: list[Path] = self.config.roots or [Path.home()]

        # Keep references so they aren't garbage-collected mid-run.
        self.worker: Optional[ScanWorker] = None
        self.dupe_worker: Optional[DuplicateWorker] = None
        self.category_worker: Optional[CategoryWorker] = None
        self.progress: Optional[QProgressDialog] = None

        self._build_ui()
        self._build_toolbar()
        self._build_statusbar()
        self._refresh_stats()

    # ---------- UI scaffolding ----------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.groups = GroupList()
        self.groups.group_selected.connect(self._on_group_selected)
        self.groups.setMinimumWidth(200)
        self.groups.setMaximumWidth(300)

        middle = QWidget()
        mid_layout = QVBoxLayout(middle)
        mid_layout.setContentsMargins(0, 0, 0, 0)

        batch_bar = QHBoxLayout()
        self.btn_select_all = QPushButton("Select all")
        self.btn_select_movable = QPushButton("Select all but suggested to keep")
        self.btn_select_movable.setToolTip(
            "Tick everything except files that look like they belong where they are."
        )
        self.btn_select_none = QPushButton("Select none")
        self.btn_invert = QPushButton("Invert")
        self.btn_select_all.clicked.connect(lambda: self._set_all(True))
        self.btn_select_movable.clicked.connect(lambda: self._set_all(True, skip_keep=True))
        self.btn_select_none.clicked.connect(lambda: self._set_all(False))
        self.btn_invert.clicked.connect(self._invert_selection)
        batch_bar.addWidget(self.btn_select_all)
        batch_bar.addWidget(self.btn_select_movable)
        batch_bar.addWidget(self.btn_select_none)
        batch_bar.addWidget(self.btn_invert)
        batch_bar.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, location or destination…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(self._apply_search)
        QShortcut(QKeySequence.StandardKey.Find, self, self.search.setFocus)
        self.search_count = QLabel()
        batch_bar.addWidget(self.search_count)
        batch_bar.addWidget(self.search)
        mid_layout.addLayout(batch_bar)

        self.table = ProposalTable()
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._edit_destination)
        mid_layout.addWidget(self.table)

        self.preview = PreviewPane()
        mid_layout.addWidget(self.preview)

        splitter.addWidget(self.groups)
        splitter.addWidget(middle)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(0, 0)
        self.setCentralWidget(splitter)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        if self.config.roots:
            scan_home = QAction("Scan configured folders", self)
            scan_home.triggered.connect(
                lambda: self._start_scan(self.config.roots, desktop_mode=False)
            )
        else:
            scan_home = QAction("Scan home folder", self)
            scan_home.triggered.connect(
                lambda: self._start_scan([Path.home()], desktop_mode=False)
            )
        tb.addAction(scan_home)

        scan_custom = QAction("Scan folder…", self)
        scan_custom.triggered.connect(self._choose_scan_root)
        tb.addAction(scan_custom)

        tb.addSeparator()

        desktop_action = QAction("Clean Desktop", self)
        desktop_action.triggered.connect(self._clean_desktop)
        tb.addAction(desktop_action)

        categories = QAction("Sort by category…", self)
        categories.triggered.connect(self._sort_by_category)
        tb.addAction(categories)

        if IS_WINDOWS:
            arrange = QAction("Arrange desktop icons", self)
            arrange.triggered.connect(self._arrange_desktop_icons)
            tb.addAction(arrange)

            restore = QAction("Restore icon layout", self)
            restore.triggered.connect(self._restore_desktop_icons)
            tb.addAction(restore)

        dupes_action = QAction("Find duplicates…", self)
        dupes_action.triggered.connect(self._find_duplicates)
        tb.addAction(dupes_action)

        tb.addSeparator()

        execute = QAction("Execute approved", self)
        execute.triggered.connect(self._execute)
        tb.addAction(execute)

        retry = QAction("Retry failed", self)
        retry.triggered.connect(self._retry_failed)
        tb.addAction(retry)

        undo = QAction("Undo last…", self)
        undo.triggered.connect(self._undo_last)
        tb.addAction(undo)

    def _build_statusbar(self) -> None:
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.stats_label = QLabel()
        self.status.addPermanentWidget(self.stats_label)

    # ---------- scanning ----------

    def _choose_scan_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to scan", str(Path.home())
        )
        if folder:
            self._start_scan([Path(folder)], desktop_mode=False)

    def _clean_desktop(self) -> None:
        self._start_scan([Path.home() / "Desktop"], desktop_mode=True)

    def _start_scan(self, roots: list[Path], *, desktop_mode: bool) -> None:
        if not self.ruleset.rules:
            QMessageBox.warning(
                self, "No rules", f"No rules loaded from {DEFAULT_RULES_PATH}."
            )
            return

        self.desktop_mode = desktop_mode
        self.scan_roots = roots
        ignored = self.db.ignored_files()

        self._open_progress("Scanning…")

        self.worker = ScanWorker(roots, self.ruleset, desktop_mode, ignored, self.config)
        self.worker.progress.connect(self._worker_progress)
        self.worker.finished_ok.connect(self._scan_done)
        self.worker.failed.connect(self._scan_failed)
        self.worker.start()

    def _scan_done(self, result, kept: int) -> None:
        if self.progress:
            self.progress.close()
        self.proposals = result.proposals
        self._populate_table()
        self._refresh_counts()
        self._refresh_stats()
        msg = (
            f"Scanned {result.scanned_files} files. "
            f"{len(self.proposals)} proposals. "
            f"Skipped: {result.skipped_cloud} cloud, "
            f"{result.skipped_project} in-projects, "
            f"{result.skipped_sorted} already sorted, "
            f"{result.skipped_system} system dirs."
        )
        if kept:
            msg += f" {kept} look like they belong where they are (unticked)."
        self.status.showMessage(msg, 15000)

    def _scan_failed(self, err: str) -> None:
        if self.progress:
            self.progress.close()
        QMessageBox.critical(self, "Scan failed", err)

    # ---------- table population ----------

    def _populate_table(self) -> None:
        self.table.blockSignals(True)
        self.table.clear_rows()
        for i, p in enumerate(self._filtered_proposals()):
            self.table.add_proposal(p, i)
        self.table.blockSignals(False)
        self._apply_search()
        self._refresh_counts()

    def _filtered_proposals(self) -> list[MoveProposal]:
        if not self.current_group:
            return self.proposals
        if self.current_group == KEEP_GROUP:
            return [p for p in self.proposals if suggests_keep(p)]
        return [p for p in self.proposals if p.rule.name == self.current_group]

    def _apply_search(self) -> None:
        """Hide rows that don't match the search box (all words, any column)."""
        words = self.search.text().lower().split()
        shown = 0
        for row in range(self.table.rowCount()):
            p = self.table.proposal_at(row)
            haystack = f"{p.file.path} {p.destination or ''} {p.rule.name}".lower()
            match = all(w in haystack for w in words)
            self.table.setRowHidden(row, not match)
            shown += match
        self.search_count.setText(
            f"{shown} of {self.table.rowCount()} match" if words else ""
        )

    def _on_group_selected(self, name: str) -> None:
        self.current_group = name
        self._populate_table()

    def _on_row_selected(self) -> None:
        self.preview.show_proposal(self.table.current_proposal())

    def _on_item_changed(self, item) -> None:
        # Keep the proposal in sync with its checkbox right away, so the
        # choice survives switching groups (the table is rebuilt from proposals).
        if item.column() == 0:
            proposal = item.data(Qt.ItemDataRole.UserRole)
            if proposal is not None:
                proposal.selected = item.checkState() == Qt.CheckState.Checked
        self._refresh_counts()

    def _edit_destination(self, row: int, column: int) -> None:
        if column != 4:
            return
        p = self.table.proposal_at(row)
        if p is None or p.action != Action.MOVE:
            return
        start = p.destination.parent if p.destination else Path.home()
        while not start.exists() and start != start.parent:
            start = start.parent
        folder = QFileDialog.getExistingDirectory(
            self, f"Move {p.file.name} into…", str(start)
        )
        if not folder:
            return
        p.destination = Path(folder) / p.file.name
        p.cross_drive = is_cross_drive(p.file.path, p.destination)
        self.table.blockSignals(True)
        self.table.refresh_row(row)
        self.table.blockSignals(False)
        self.preview.show_proposal(p)

    # ---------- batch actions ----------

    def _set_all(self, checked: bool, *, skip_keep: bool = False) -> None:
        """Tick/untick every visible row. skip_keep: untick "suggested to keep" rows instead."""
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item or self.table.isRowHidden(row):
                continue
            proposal = item.data(Qt.ItemDataRole.UserRole)
            if proposal.action == Action.SKIP:
                continue
            tick = checked and not (skip_keep and suggests_keep(proposal))
            item.setCheckState(
                Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked
            )
            proposal.selected = tick
        self.table.blockSignals(False)
        self._refresh_counts()

    def _invert_selection(self) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item or self.table.isRowHidden(row):
                continue
            proposal = item.data(Qt.ItemDataRole.UserRole)
            if proposal.action == Action.SKIP:
                continue
            new = item.checkState() != Qt.CheckState.Checked
            item.setCheckState(
                Qt.CheckState.Checked if new else Qt.CheckState.Unchecked
            )
            proposal.selected = new
        self.table.blockSignals(False)
        self._refresh_counts()

    # ---------- execute ----------

    def _execute(self) -> None:
        approved = [
            p for p in self.table.proposals()
            if p.selected and p.action != Action.SKIP
        ]
        visible_ids = {id(p) for p in approved}
        for p in self.proposals:
            if id(p) not in visible_ids and p.selected and p.action != Action.SKIP:
                approved.append(p)

        if not approved:
            QMessageBox.information(self, "Nothing to do", "No files selected.")
            return

        for p in approved:
            if p.destination is None and p.action == Action.MOVE:
                dest = QFileDialog.getExistingDirectory(
                    self,
                    f"Destination for folder: {p.file.name}",
                    str(Path.home()),
                )
                if not dest:
                    p.selected = False
                    continue
                p.destination = Path(dest) / p.file.name

        approved = [p for p in approved if p.selected]
        if not approved:
            return

        total_bytes = sum(p.file.size for p in approved)
        cross = sum(1 for p in approved if p.cross_drive)
        summary = (
            f"About to process {len(approved)} items, {human_size(total_bytes)} total.\n"
            f"{cross} will cross drives (slow).\n\nProceed?"
        )
        if QMessageBox.question(self, "Confirm", summary) != QMessageBox.StandardButton.Yes:
            return

        results = execute_batch(approved, name_conflict=self.config.name_conflict)
        succeeded = sum(1 for r in results if r.success)
        failed = [r for r in results if not r.success]

        for r in results:
            self.db.log_result(r, rule_name=r.proposal.rule.name)

        self.retry_queue = [r.proposal for r in failed]

        msg = f"Done. {succeeded} succeeded, {len(failed)} failed."
        if failed:
            msg += "\n\nFailures:\n" + "\n".join(
                f"• {r.proposal.file.name}: {r.error}" for r in failed[:5]
            )
            if len(failed) > 5:
                msg += f"\n…and {len(failed) - 5} more."
        QMessageBox.information(self, "Results", msg)

        succeeded_ids = {id(r.proposal) for r in results if r.success}
        self.proposals = [p for p in self.proposals if id(p) not in succeeded_ids]
        self._populate_table()
        self._refresh_stats()

    def _retry_failed(self) -> None:
        if not self.retry_queue:
            QMessageBox.information(self, "Retry", "No failed items to retry.")
            return
        results = execute_batch(self.retry_queue, name_conflict=self.config.name_conflict)
        succeeded = [r for r in results if r.success]
        for r in results:
            self.db.log_result(r, rule_name=r.proposal.rule.name)
        self.retry_queue = [r.proposal for r in results if not r.success]
        QMessageBox.information(
            self, "Retry",
            f"{len(succeeded)} succeeded, {len(self.retry_queue)} still failing.",
        )
        self._refresh_stats()

    # ---------- duplicates ----------

    def _find_duplicates(self) -> None:
        roots = self.scan_roots or [Path.home()]

        self._open_progress("Hashing files…")

        self.dupe_worker = DuplicateWorker(roots, self.config)
        self.dupe_worker.finished_ok.connect(self._duplicates_done)
        self.dupe_worker.failed.connect(self._duplicates_failed)
        self.dupe_worker.start()

    def _duplicates_done(self, result) -> None:
        if self.progress:
            self.progress.close()

        if not result.groups:
            QMessageBox.information(self, "Duplicates", "No duplicates found.")
            return

        self._show_duplicate_dialog(result)

    def _duplicates_failed(self, err: str) -> None:
        if self.progress:
            self.progress.close()
        QMessageBox.critical(self, "Duplicate scan failed", err)

    def _show_duplicate_dialog(self, result) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Duplicates")
        dlg.resize(1100, 700)
        layout = QVBoxLayout(dlg)

        summary = QLabel(
            f"{len(result.groups)} duplicate groups, "
            f"{sum(g.count for g in result.groups)} files total, "
            f"{human_size(result.total_wasted)} recoverable."
        )
        layout.addWidget(summary)

        tree = QTreeWidget()
        tree.setHeaderLabels(["File", "Size", "Path", "Keep?"])
        tree.setColumnWidth(0, 300)
        tree.setColumnWidth(1, 100)
        tree.setColumnWidth(2, 500)
        layout.addWidget(tree)

        keep_checkboxes: dict[Path, QTreeWidgetItem] = {}

        for g in result.groups:
            top = QTreeWidgetItem(
                [
                    f"Group: {g.count} files × {human_size(g.size)}"
                    f"  ({human_size(g.wasted_bytes)} recoverable)",
                    "", "", "",
                ]
            )
            top.setExpanded(True)
            tree.addTopLevelItem(top)

            for f in g.files:
                child = QTreeWidgetItem(
                    [f.name, human_size(f.size), str(f.path), ""]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, f)
                if f is g.keeper:
                    child.setCheckState(3, Qt.CheckState.Checked)
                else:
                    child.setCheckState(3, Qt.CheckState.Unchecked)
                top.addChild(child)
                keep_checkboxes[f.path] = child

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        to_trash: list[FileInfo] = []
        for g in result.groups:
            keepers = [
                f for f in g.files
                if keep_checkboxes[f.path].checkState(3) == Qt.CheckState.Checked
            ]
            if len(keepers) != 1:
                # User didn't pick exactly one keeper — skip this group.
                continue
            keeper = keepers[0]
            for f in g.files:
                if f is not keeper:
                    to_trash.append(f)

        if not to_trash:
            return

        total = sum(f.size for f in to_trash)
        if QMessageBox.question(
            self, "Confirm",
            f"Trash {len(to_trash)} files, freeing {human_size(total)}?\n"
            f"They will go to your OS trash (recoverable).",
        ) != QMessageBox.StandardButton.Yes:
            return

        try:
            from send2trash import send2trash
        except ImportError:
            QMessageBox.critical(
                self, "Missing dependency",
                "send2trash is not installed. Run: pip install Send2Trash",
            )
            return

        trash_rule = Rule(
            name="Duplicate", priority=0, destination=None, action=Action.TRASH
        )
        succeeded = 0
        for f in to_trash:
            proposal = MoveProposal(
                file=f,
                rule=trash_rule,
                destination=None,
                action=Action.TRASH,
                reason=MatchReason.DUPLICATE,
                cross_drive=False,
                selected=True,
            )
            try:
                send2trash(str(f.path))
                self.db.log_result(
                    MoveResult(proposal=proposal, success=True),
                    rule_name="Duplicate",
                )
                succeeded += 1
            except Exception as e:
                self.db.log_result(
                    MoveResult(proposal=proposal, success=False, error=str(e)),
                    rule_name="Duplicate",
                )

        QMessageBox.information(self, "Done", f"Trashed {succeeded} files.")
        self._refresh_stats()

    # ---------- sort by category ----------

    def _sort_by_category(self) -> None:
        dlg = CategoryDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.scan_root is None:
            return

        self.scan_roots = [dlg.scan_root]
        self._open_progress("Finding files…", "Sort by category")

        self.category_worker = CategoryWorker(
            dlg.scan_root, dlg.categories(), self.config, self.db.ignored_files()
        )
        self.category_worker.progress.connect(self._worker_progress)
        self.category_worker.finished_ok.connect(self._category_done)
        self.category_worker.failed.connect(self._scan_failed)
        self.category_worker.start()

    def _worker_progress(self, stage: str, done: int, total: int) -> None:
        if not self.progress:
            return
        if stage == "Loading model":
            stage = "Loading model (downloads ~210 MB the first time)"
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
            note = "  — large folder, this may take several minutes" if total > LARGE_SCAN_FILES else ""
            self.progress.setLabelText(f"{stage}: {done} / {total}{note}")
        else:
            self.progress.setMaximum(0)
            self.progress.setLabelText(f"{stage}…")

    def _category_done(self, proposals: list, examined: int, kept: int) -> None:
        if self.progress:
            self.progress.close()
        self.desktop_mode = False
        self.current_group = ""
        self.proposals = proposals
        self._populate_table()
        self._refresh_stats()
        self.status.showMessage(
            f"Looked at {examined} files; {len(proposals)} matched a category. "
            "Unmatched or unclear files are left where they are."
            + (f" {kept} look like they belong where they are (unticked)." if kept else ""),
            15000,
        )

    # ---------- desktop icon layout ----------

    def _arrange_desktop_icons(self) -> None:
        if QMessageBox.question(
            self, "Arrange desktop icons",
            "Group the icons on your desktop by type (folders, shortcuts, "
            "documents, pictures, …), alphabetically, in columns from the top-left?\n\n"
            "Your current layout is saved first — use \"Restore icon layout\" to undo. "
            "Auto-arrange will be turned off so the layout sticks.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            counts = desktop_icons.arrange_desktop()
        except desktop_icons.DesktopIconError as e:
            QMessageBox.warning(self, "Arrange desktop icons", str(e))
            return
        summary = ", ".join(f"{n} {cat.lower()}" for cat, n in counts.items())
        self.status.showMessage(f"Arranged desktop icons: {summary}.", 15000)

    def _restore_desktop_icons(self) -> None:
        if not desktop_icons.has_saved_layout():
            QMessageBox.information(self, "Restore icon layout", "No saved layout yet.")
            return
        try:
            moved = desktop_icons.restore_layout()
        except desktop_icons.DesktopIconError as e:
            QMessageBox.warning(self, "Restore icon layout", str(e))
            return
        self.status.showMessage(f"Restored {moved} desktop icons to their previous spots.", 15000)

    # ---------- undo ----------

    def _undo_last(self) -> None:
        rows = self.db.recent_actions(limit=20)
        if not rows:
            QMessageBox.information(self, "Undo", "Nothing to undo.")
            return

        labels = [
            f"[{r['timestamp']}] {r['action']}: "
            f"{Path(r['src']).name} → {r['dst'] or 'trash'}"
            for r in rows
        ]
        choice, ok = QInputDialog.getItem(
            self, "Undo", "Pick an action to undo:", labels, 0, False
        )
        if not ok:
            return
        idx = labels.index(choice)
        row = rows[idx]

        if row["action"] == "move" and row["dst"]:
            success, err = undo_move(Path(row["src"]), Path(row["dst"]))
            if success:
                self.db.mark_undone(row["id"])
                QMessageBox.information(self, "Undo", "Restored.")
            else:
                QMessageBox.warning(self, "Undo failed", err or "Unknown error")
        elif row["action"] == "trash":
            QMessageBox.information(
                self,
                "Undo trash",
                "Trashed files must be restored from your OS trash manually.\n"
                f"Original path: {row['src']}",
            )
        self._refresh_stats()

    # ---------- helpers ----------

    def _open_progress(self, text: str, title: str = "File Janitor") -> None:
        self.progress = QProgressDialog(text, None, 0, 0, self)
        self.progress.setWindowTitle(title)
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setCancelButton(None)
        self.progress.setMinimumDuration(0)
        # Workers report several stages; a stage hitting 100% (e.g. "Reading
        # documents 33/33") must not close the dialog while the next one runs.
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.progress.show()

    def _refresh_counts(self) -> None:
        counts: dict[str, int] = {}
        for p in self.proposals:
            counts[p.rule.name] = counts.get(p.rule.name, 0) + 1
        self.groups.set_groups(counts, sum(1 for p in self.proposals if suggests_keep(p)))

    def _refresh_stats(self) -> None:
        s = self.db.stats()
        self.stats_label.setText(
            f"Moved: {s['moved']}   Trashed: {s['trashed']}   "
            f"Handled: {human_size(s['bytes'])}"
        )

    def closeEvent(self, event) -> None:
        # Wait for any running worker to finish cleanly.
        for w in (self.worker, self.dupe_worker, self.category_worker):
            if w is not None and w.isRunning():
                w.wait(2000)
        self.db.close()
        super().closeEvent(event)