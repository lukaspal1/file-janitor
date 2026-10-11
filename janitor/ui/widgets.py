"""
Small reusable widgets for the main window.
"""
from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPoint, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..context import KEEP_THRESHOLD


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


KEEP_GROUP = "__keep__"     # GroupList entry for "Suggested to keep"
KEEP_TINT = QColor(230, 160, 0, 70)


def suggests_keep(proposal) -> bool:
    return proposal.keep_score >= KEEP_THRESHOLD


def reveal_in_file_manager(path: Path) -> None:
    """Open the OS file manager at *path*, selecting it if it's a file.

    If *path* doesn't exist (e.g. a destination that hasn't been created yet),
    opens the nearest existing parent folder instead.
    """
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    if not target.exists():
        return

    if sys.platform == "win32" and target.is_file():
        subprocess.Popen(["explorer", "/select,", str(target)])
    elif sys.platform == "darwin" and target.is_file():
        subprocess.Popen(["open", "-R", str(target)])
    else:
        folder = target if target.is_dir() else target.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class GroupList(QListWidget):
    """Left sidebar listing rule groups with counts."""

    group_selected = pyqtSignal(str)  # rule name, or "" for all

    def __init__(self, parent=None):
        super().__init__(parent)
        self.itemClicked.connect(self._on_click)

    def set_groups(self, counts: dict[str, int], keep_count: int = 0) -> None:
        self.clear()
        all_item = QListWidgetItem(f"All files ({sum(counts.values())})")
        all_item.setData(Qt.ItemDataRole.UserRole, "")
        self.addItem(all_item)

        if keep_count:
            keep_item = QListWidgetItem(f"Suggested to keep ({keep_count})")
            keep_item.setData(Qt.ItemDataRole.UserRole, KEEP_GROUP)
            keep_item.setToolTip(
                "Files that look like they belong where they are — part of a "
                "project, a program's data, or used by a nearby file. "
                "They start unticked."
            )
            keep_item.setBackground(KEEP_TINT)
            self.addItem(keep_item)

        for name, count in sorted(counts.items(), key=lambda x: -x[1]):
            item = QListWidgetItem(f"{name} ({count})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.addItem(item)

    def _on_click(self, item: QListWidgetItem) -> None:
        self.group_selected.emit(item.data(Qt.ItemDataRole.UserRole) or "")


class ProposalTable(QTableWidget):
    """Middle pane — one row per proposal."""

    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(
            ["", "Name", "Size", "Current location", "Destination"]
        )
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 28)
        self.setColumnWidth(1, 240)
        self.setColumnWidth(2, 90)
        self.setColumnWidth(3, 340)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint) -> None:
        row = self.rowAt(pos.y())
        proposal = self.proposal_at(row) if row >= 0 else None
        if proposal is None:
            return

        menu = QMenu(self)
        menu.addAction(
            "Show in file manager",
            lambda: reveal_in_file_manager(proposal.file.path),
        )
        if proposal.destination:
            menu.addAction(
                "Open destination folder",
                lambda: reveal_in_file_manager(proposal.destination.parent),
            )
        menu.addSeparator()
        menu.addAction(
            "Copy path",
            lambda: QGuiApplication.clipboard().setText(str(proposal.file.path)),
        )
        if proposal.destination:
            menu.addAction(
                "Copy destination path",
                lambda: QGuiApplication.clipboard().setText(str(proposal.destination)),
            )
        menu.exec(self.viewport().mapToGlobal(pos))

    def clear_rows(self) -> None:
        self.setRowCount(0)

    def add_proposal(self, proposal, row_index: int) -> None:
        self.insertRow(row_index)

        # Checkbox
        cb = QTableWidgetItem()
        cb.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        cb.setCheckState(
            Qt.CheckState.Checked if proposal.selected else Qt.CheckState.Unchecked
        )
        cb.setData(Qt.ItemDataRole.UserRole, proposal)
        self.setItem(row_index, 0, cb)

        self.setItem(row_index, 1, QTableWidgetItem())
        self.setItem(row_index, 2, QTableWidgetItem(human_size(proposal.file.size)))
        self.setItem(row_index, 3, QTableWidgetItem(str(proposal.file.parent)))
        self.setItem(row_index, 4, QTableWidgetItem())
        self.refresh_row(row_index)

        if proposal.action.value == "skip":
            for col in range(5):
                item = self.item(row_index, col)
                if item:
                    item.setForeground(Qt.GlobalColor.gray)

    def refresh_row(self, row: int) -> None:
        """Re-render the name and destination cells after the proposal changed."""
        proposal = self.proposal_at(row)
        if proposal is None:
            return
        name = proposal.file.name
        if proposal.cross_drive:
            name += "   [cross-drive]"
        name_item = self.item(row, 1)
        name_item.setText(name)
        if suggests_keep(proposal):
            name_item.setBackground(KEEP_TINT)
            name_item.setToolTip(
                "Suggested to keep where it is:\n• " + "\n• ".join(proposal.keep_reasons)
            )

        if proposal.destination:
            dest_text = str(proposal.destination)
        elif proposal.action.value == "move":
            dest_text = "(double-click to choose)"
        else:
            dest_text = ""
        dest_item = self.item(row, 4)
        dest_item.setText(dest_text)
        if proposal.action.value == "move":
            dest_item.setToolTip("Double-click to change the destination folder.")

    def proposal_at(self, row: int):
        item = self.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def proposals(self) -> list:
        out = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item:
                p = item.data(Qt.ItemDataRole.UserRole)
                p.selected = item.checkState() == Qt.CheckState.Checked
                out.append(p)
        return out

    def current_proposal(self):
        row = self.currentRow()
        if row < 0:
            return None
        return self.proposal_at(row)


class PreviewPane(QWidget):
    """Bottom pane — shows details for the currently selected proposal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(140)
        self.setMaximumHeight(220)

        # Thumbnail on the left, details on the right; details scroll when long.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.thumb = QLabel()
        self.thumb.setFixedSize(128, 96)
        self.thumb.setStyleSheet("border: 1px solid #444;")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)

        self.info = QLabel("Select a file to preview.")
        self.info.setWordWrap(True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self.info)
        layout.addWidget(scroll, 1)

    @staticmethod
    def _keep_html(proposal) -> str:
        if not proposal.keep_reasons:
            return ""
        verdict = (
            "<b>Suggested to keep where it is</b>" if suggests_keep(proposal)
            else "Might belong where it is"
        )
        reasons = "".join(f"<br>&nbsp;&nbsp;• {html.escape(r)}" for r in proposal.keep_reasons)
        return f"<br><br>{verdict} ({proposal.keep_score:.0%}):{reasons}"

    def show_proposal(self, proposal) -> None:
        if proposal is None:
            self.thumb.clear()
            self.info.setText("Select a file to preview.")
            return

        f = proposal.file
        self.info.setText(
            f"<b>{f.name}</b><br>"
            f"Size: {human_size(f.size)}<br>"
            f"Path: {f.path}<br>"
            f"Rule: {proposal.rule.name}<br>"
            f"Action: {proposal.action.value}"
            + (" (cross-drive)" if proposal.cross_drive else "")
            + (f"<br>{proposal.rule.description}" if proposal.rule.description else "")
            + self._keep_html(proposal)
        )

        self.thumb.clear()
        if f.ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            pix = QPixmap(str(f.path))
            if not pix.isNull():
                self.thumb.setPixmap(
                    pix.scaled(
                        self.thumb.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.thumb.setText("(image)")
        else:
            self.thumb.setText(f.ext or "file")