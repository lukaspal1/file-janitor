"""
Execute MoveProposals safely.

Rules:
- MOVE within the same drive: shutil.move
- MOVE across drives: shutil.move (falls back to copy+delete automatically)
  but only if the proposal is explicitly approved (selected=True).
- TRASH: send2trash. Never os.remove.
- Name collisions: rename with (1), (2) ... and log both source and final dst
  so undo can find the file again.
- Locked files: report the error, do not retry. The UI offers a "retry" button.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover
    send2trash = None  # type: ignore

from .models import Action, MoveProposal, MoveResult


def _unique_destination(dst: Path) -> Path:
    """
    If dst exists, return dst with a (1), (2), ... suffix.

    report.pdf  ->  report (1).pdf  ->  report (2).pdf
    """
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def execute(proposal: MoveProposal, *, name_conflict: str = "rename") -> MoveResult:
    """
    Perform one proposal. Caller is responsible for deciding whether
    to execute cross-drive moves.
    """
    src = proposal.file.path
    action = proposal.action

    if action == Action.SKIP:
        return MoveResult(proposal=proposal, success=True, actual_destination=None)

    if not src.exists():
        return MoveResult(
            proposal=proposal,
            success=False,
            error=f"Source no longer exists: {src}",
        )

    # ---- TRASH ----
    if action == Action.TRASH:
        if send2trash is None:
            return MoveResult(
                proposal=proposal,
                success=False,
                error="send2trash is not installed (pip install Send2Trash)",
            )
        try:
            send2trash(str(src))
            return MoveResult(proposal=proposal, success=True, actual_destination=None)
        except Exception as e:
            return MoveResult(proposal=proposal, success=False, error=str(e))

    # ---- MOVE ----
    if action == Action.MOVE:
        if proposal.destination is None:
            return MoveResult(
                proposal=proposal,
                success=False,
                error="No destination set on proposal",
            )

        dst = proposal.destination
        src_res, dst_res = src.resolve(), dst.resolve()
        if src_res == dst_res:
            return MoveResult(proposal=proposal, success=False, error="Already at destination")
        if dst_res.is_relative_to(src_res):
            return MoveResult(
                proposal=proposal,
                success=False,
                error=f"Cannot move a folder into itself: {dst}",
            )

        try:
            _ensure_parent(dst)
        except OSError as e:
            return MoveResult(proposal=proposal, success=False, error=f"Cannot create {dst.parent}: {e}")

        if dst.exists():
            if name_conflict == "skip":
                return MoveResult(
                    proposal=proposal,
                    success=False,
                    error=f"Destination exists: {dst}",
                    actual_destination=dst,
                )
            dst = _unique_destination(dst)

        try:
            # shutil.move handles cross-drive automatically (copy + unlink).
            shutil.move(str(src), str(dst))
        except Exception as e:
            return MoveResult(
                proposal=proposal,
                success=False,
                error=f"{type(e).__name__}: {e}",
            )

        return MoveResult(proposal=proposal, success=True, actual_destination=dst)

    return MoveResult(proposal=proposal, success=False, error=f"Unknown action: {action}")


def execute_batch(
    proposals: list[MoveProposal],
    *,
    name_conflict: str = "rename",
    progress_cb=None,
) -> list[MoveResult]:
    """
    Execute every proposal with selected=True.
    progress_cb(i, total, proposal) is called before each one.
    """
    results: list[MoveResult] = []
    active = [p for p in proposals if p.selected and p.action != Action.SKIP]
    total = len(active)
    for i, p in enumerate(active, 1):
        if progress_cb:
            progress_cb(i, total, p)
        results.append(execute(p, name_conflict=name_conflict))
    return results


# ---------- undo ----------

def undo_move(src: Path, dst: Path) -> tuple[bool, Optional[str]]:
    """
    Move dst back to src. Used by the undo flow.
    Returns (success, error_message).
    """
    if not dst.exists():
        return False, f"File no longer at {dst}"
    if src.exists():
        return False, f"Original location already occupied: {src}"
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"