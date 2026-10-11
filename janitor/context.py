"""
"Does this file belong where it already is?"

Runs after a scan, over every proposal, and collects reasons to leave a
file in place. Each signal has a weight; they combine like independent
probabilities (1 - Π(1 - w)). Proposals that clear KEEP_THRESHOLD are
unticked and show their reasons in the UI — the user can still tick them.

Signals:
  - project:    a project / IDE workspace marker in a parent folder
                (.git, .project, *.sln, package.json, …)
  - app:        a parent folder holds programs (.exe / .dll) — a portable
                app or game whose files must stay together
  - app data:   a parent folder is a game's or program's data folder in
                Documents (My Games, Image-Line, …, or one with settings/,
                saves/, profiles/ … inside)
  - layout:     a parent folder is laid out like a project or program
                (two or more of src/, bin/, include/, lib/, …)
  - code:       the folder itself holds several source files (loose
                assignment / sample-code folders with no project file)
  - referenced: a nearby text/code file mentions the file by name
                (index.html -> logo.png, stylesheet.css -> x.png)
  - structure:  the folder has a typical project-part name (src, doc,
                resources, assets, …)
  - related:    files with the same base name sit next to it and aren't
                going to the same place (report.pdf + report.docx)
  - content:    the local embedding model thinks the document's text fits
                the folder it's in (optional; documents only)

Standard containers (home, Desktop, Downloads, …) are never treated as
"where it belongs" — tidying those is the whole point of the app.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import (
    APP_DATA_DIR_NAMES,
    APP_DATA_HINT_DIRS,
    CONTAINER_DIR_NAMES,
    EXT_CODE,
    PROJECT_MARKERS,
)
from .models import Action, MoveProposal

KEEP_THRESHOLD = 0.5

W_PROJECT = 0.7
W_APP = 0.7
W_APP_DATA = 0.8
W_LAYOUT = 0.55
W_CODE = 0.6
W_REFERENCED = 0.7
W_STRUCTURE = 0.35
W_RELATED = 0.35
W_CONTENT = 0.45

# How far up to look for a project marker / referencing files.
PROJECT_SEARCH_DEPTH = 6
REFERENCE_SEARCH_DEPTH = 3

# Folder names that usually mean "part of something bigger".
STRUCTURE_DIR_NAMES = {
    "src", "source", "lib", "libs", "bin", "obj", "build", "dist", "out",
    "doc", "docs", "documentation", "javadoc", "res", "resources", "assets",
    "static", "public", "include", "test", "tests", "spec", "fixtures",
    "vendor", "third_party", "img", "imgs", "css", "js", "fonts", "icons",
    "sprites", "textures", "locale", "locales", "templates", "layouts",
}

# Subfolders that, two or more together, mean "this is a project/program".
LAYOUT_DIR_NAMES = {
    "src", "bin", "include", "lib", "build", "dist", "obj", "out", "target",
    "test", "tests", "doc", "docs", "html", "resources", "assets", "jmods",
    "conf", "node_modules", "scripts",
}
LAYOUT_MIN_DIRS = 2
CODE_MIN_FILES = 3

# Files that can mention other files by name.
REFERENCE_EXTS = EXT_CODE | {
    ".htm", ".xml", ".json", ".md", ".tex", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".csv", ".svg", ".xhtml", ".properties",
}
MAX_REF_FILES_PER_DIR = 200
MAX_REF_BYTES = 512 * 1024

_MARKER_NAMES = {m for m in PROJECT_MARKERS if "*" not in m}
_MARKER_GLOBS = tuple(m for m in PROJECT_MARKERS if "*" in m)
APP_EXTS = {".exe", ".dll"}
_APP_DATA_EXACT = {n.lower() for n in APP_DATA_DIR_NAMES if not n.endswith("*")}
_APP_DATA_PREFIX = tuple(n[:-1].lower() for n in APP_DATA_DIR_NAMES if n.endswith("*"))

# Content signal: cosine between the document and its folder description.
# bge scores are compressed; below CONTENT_LOW counts for nothing.
CONTENT_LOW = 0.55
CONTENT_HIGH = 0.70


@dataclass
class KeepAdvice:
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def add(self, weight: float, reason: str) -> None:
        self.score = 1 - (1 - self.score) * (1 - weight)
        self.reasons.append(reason)


def _containers() -> set[Path]:
    home = Path.home()
    return {home, home.parent, Path(home.anchor)} | {home / n for n in CONTAINER_DIR_NAMES}


class _Context:
    """Per-run caches so each directory is listed/read at most once."""

    def __init__(self) -> None:
        self.containers = {p.resolve() for p in _containers()}
        self._listing: dict[Path, list[os.DirEntry]] = {}
        self._project: dict[Path, Optional[tuple[Path, str]]] = {}
        self._app: dict[Path, Optional[Path]] = {}
        self._app_data: dict[Path, Optional[tuple[Path, str]]] = {}
        self._layout: dict[Path, Optional[tuple[Path, list[str]]]] = {}
        self._resolved: dict[Path, Path] = {}
        self._ref_text: dict[Path, list[tuple[str, str]]] = {}

    def is_container(self, d: Path) -> bool:
        return d in self.containers or d == d.parent

    def resolve(self, d: Path) -> Path:
        if d not in self._resolved:
            self._resolved[d] = d.resolve()
        return self._resolved[d]

    def listing(self, d: Path) -> list[os.DirEntry]:
        if d not in self._listing:
            try:
                self._listing[d] = list(os.scandir(d))
            except OSError:
                self._listing[d] = []
        return self._listing[d]

    def _marker_in(self, d: Path) -> Optional[str]:
        for e in self.listing(d):
            if e.name in _MARKER_NAMES:
                return e.name
            if any(fnmatch.fnmatch(e.name, g) for g in _MARKER_GLOBS):
                return e.name
        return None

    def project_root(self, d: Path, depth: int = PROJECT_SEARCH_DEPTH) -> Optional[tuple[Path, str]]:
        """Nearest ancestor (inclusive) with a project marker, stopping at containers."""
        if depth == 0 or self.is_container(d):
            return None
        if d not in self._project:
            marker = self._marker_in(d)
            self._project[d] = (d, marker) if marker else self.project_root(d.parent, depth - 1)
        return self._project[d]

    def app_root(self, d: Path, depth: int = PROJECT_SEARCH_DEPTH) -> Optional[Path]:
        """Nearest ancestor (inclusive) holding an .exe/.dll, stopping at containers."""
        if depth == 0 or self.is_container(d):
            return None
        if d not in self._app:
            has_app = any(
                os.path.splitext(e.name)[1].lower() in APP_EXTS for e in self.listing(d)
            )
            self._app[d] = d if has_app else self.app_root(d.parent, depth - 1)
        return self._app[d]

    def app_data_root(self, d: Path, depth: int = PROJECT_SEARCH_DEPTH) -> Optional[tuple[Path, str]]:
        """Nearest ancestor (inclusive) that is a program's data folder."""
        if depth == 0 or self.is_container(d):
            return None
        if d not in self._app_data:
            name = d.name.lower()
            hint = next(
                (e.name for e in self.listing(d)
                 if e.name.lower() in APP_DATA_HINT_DIRS and e.is_dir()),
                None,
            )
            if name in _APP_DATA_EXACT or name.startswith(_APP_DATA_PREFIX):
                found = (d, "a known game/app data folder")
            elif hint:
                found = (d, f"has a '{hint}' folder")
            else:
                found = self.app_data_root(d.parent, depth - 1)
            self._app_data[d] = found
        return self._app_data[d]

    def layout_root(self, d: Path, depth: int = PROJECT_SEARCH_DEPTH) -> Optional[tuple[Path, list[str]]]:
        """Nearest ancestor (inclusive) with several project-style subfolders."""
        if depth == 0 or self.is_container(d):
            return None
        if d not in self._layout:
            hits = sorted(
                e.name for e in self.listing(d)
                if e.name.lower() in LAYOUT_DIR_NAMES and e.is_dir()
            )
            self._layout[d] = (
                (d, hits) if len(hits) >= LAYOUT_MIN_DIRS
                else self.layout_root(d.parent, depth - 1)
            )
        return self._layout[d]

    def source_count(self, d: Path) -> int:
        return sum(
            1 for e in self.listing(d)
            if os.path.splitext(e.name)[1].lower() in EXT_CODE
        )

    def ref_texts(self, d: Path) -> list[tuple[str, str]]:
        """(file name, lowercased text) for the referencing-type files in d."""
        if d not in self._ref_text:
            out = []
            for e in self.listing(d):
                if len(out) >= MAX_REF_FILES_PER_DIR:
                    break
                if Path(e.name).suffix.lower() not in REFERENCE_EXTS:
                    continue
                try:
                    if not e.is_file():
                        continue
                    with open(e.path, "r", encoding="utf-8", errors="ignore") as fh:
                        out.append((e.name, fh.read(MAX_REF_BYTES).lower()))
                except OSError:
                    continue
            self._ref_text[d] = out
        return self._ref_text[d]


def _referenced_by(ctx: _Context, folder: Path, filename: str) -> Optional[str]:
    """Name of a nearby file that mentions `filename` by name, if any."""
    name = filename.lower()
    pattern = re.compile(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w\-])")
    d = folder
    for _ in range(REFERENCE_SEARCH_DEPTH):
        if ctx.is_container(d):
            break
        for ref_name, text in ctx.ref_texts(d):
            if d == folder and ref_name.lower() == name:
                continue
            if name in text and pattern.search(text):
                return ref_name if d == folder else f"{d.name}/{ref_name}"
        d = d.parent
    return None


def _related_siblings(ctx: _Context, folder: Path, p: MoveProposal,
                      moving_together: dict[Path, Optional[Path]]) -> list[str]:
    """Same-stem siblings that aren't heading to the same folder."""
    stem = p.file.path.stem.lower()
    if len(stem) < 3:
        return []
    dest_dir = p.destination.parent if p.destination else None
    out = []
    for e in ctx.listing(folder):
        if e.name == p.file.name or Path(e.name).stem.lower() != stem:
            continue
        sib = p.file.parent / e.name
        if sib in moving_together and moving_together[sib] == dest_dir:
            continue
        out.append(e.name)
    return out


def _folder_label(ctx: _Context, d: Path, max_parts: int = 4) -> str:
    parts = []
    while not ctx.is_container(d) and len(parts) < max_parts:
        parts.append(d.name)
        d = d.parent
    return " > ".join(reversed(parts))


def assess(proposals: list[MoveProposal], *, use_model: bool = True,
           progress_cb: Optional[Callable[[str, int, int], None]] = None) -> int:
    """
    Fill in keep_score / keep_reasons on each proposal and untick the ones
    that look like they belong where they are. Returns how many were flagged.
    """
    ctx = _Context()
    moving_together = {
        p.file.path: (p.destination.parent if p.destination else None)
        for p in proposals if p.action != Action.SKIP
    }
    advice: dict[int, KeepAdvice] = {}

    candidates = [p for p in proposals if p.action != Action.SKIP]
    for i, p in enumerate(candidates, 1):
        if progress_cb and i % 200 == 0:
            progress_cb("Checking where files belong", i, len(candidates))
        folder = ctx.resolve(p.file.parent)
        if ctx.is_container(folder):
            continue
        a = KeepAdvice()

        proj = ctx.project_root(folder)
        if proj:
            root, marker = proj
            a.add(W_PROJECT, f"Part of a project ({marker} in {root.name})")

        app = ctx.app_root(folder)
        if app:
            a.add(W_APP, f"Inside a program folder ({app.name} has .exe/.dll files)")

        app_data = ctx.app_data_root(folder) if not (proj or app) else None
        if app_data:
            root, why = app_data
            a.add(W_APP_DATA, f"Inside program data: {root.name} ({why})")

        layout = ctx.layout_root(folder) if not (proj or app or app_data) else None
        if layout:
            root, dirs = layout
            a.add(W_LAYOUT, f"{root.name} is laid out like a project ({', '.join(dirs[:3])})")

        n_src = ctx.source_count(folder) if not (proj or app or app_data) else 0
        if n_src >= CODE_MIN_FILES:
            a.add(W_CODE, f"Folder holds {n_src} source files")

        ref = _referenced_by(ctx, folder, p.file.name)
        if ref:
            a.add(W_REFERENCED, f"Referenced by {ref}")

        if folder.name.lower() in STRUCTURE_DIR_NAMES:
            a.add(W_STRUCTURE, f"In a '{folder.name}' folder, typical of projects")

        related = _related_siblings(ctx, folder, p, moving_together)
        if related:
            shown = ", ".join(related[:3]) + ("…" if len(related) > 3 else "")
            a.add(W_RELATED, f"Sits with related files: {shown}")

        advice[id(p)] = a

    if use_model:
        _content_signal(ctx, candidates, advice, progress_cb)

    flagged = 0
    for p in candidates:
        a = advice.get(id(p))
        if a is None:
            continue
        p.keep_score = a.score
        p.keep_reasons = a.reasons
        if a.score >= KEEP_THRESHOLD:
            p.selected = False
            flagged += 1
    return flagged


def _content_signal(ctx: _Context, candidates: list[MoveProposal],
                    advice: dict[int, KeepAdvice],
                    progress_cb: Optional[Callable[[str, int, int], None]]) -> None:
    """Ask the local model whether each document's text fits its folder."""
    from .categories import TEXT_EXTS, summarize

    exts = TEXT_EXTS | {".pdf", ".docx"}
    todo = [
        p for p in candidates
        if id(p) in advice and p.file.ext in exts
        and advice[id(p)].score < KEEP_THRESHOLD     # already decided otherwise
    ]
    if not todo:
        return

    docs, keep = [], []
    for i, p in enumerate(todo, 1):
        if progress_cb:
            progress_cb("Reading documents", i, len(todo))
        doc = summarize(p.file.path)
        if doc.from_content:
            # The summary names the folder; drop it so we compare content, not names.
            docs.append(doc.text.replace(f" Folder: {p.file.parent.name}.", "", 1))
            keep.append(p)
    if not docs:
        return

    if progress_cb:
        progress_cb("Loading model", 0, 0)
    try:
        model, norm = _model()
    except Exception:
        return          # model unavailable — heuristics still apply

    labels = {p.file.parent: _folder_label(ctx, ctx.resolve(p.file.parent)) for p in keep}
    folders = sorted({l for l in labels.values() if l})
    if not folders:
        return
    if progress_cb:
        progress_cb("Comparing documents to their folders", 0, 0)
    fvecs = dict(zip(folders, (norm(v) for v in model.query_embed(
        [f"Files about {f}" for f in folders]))))
    dvecs = [norm(v) for v in model.passage_embed(docs, batch_size=16)]

    for p, vec in zip(keep, dvecs):
        label = labels[p.file.parent]
        if not label:
            continue
        sim = float(vec @ fvecs[label])
        if sim <= CONTENT_LOW:
            continue
        strength = min(1.0, (sim - CONTENT_LOW) / (CONTENT_HIGH - CONTENT_LOW))
        advice[id(p)].add(W_CONTENT * strength,
                          f"Content fits its folder '{label}' (AI match {sim:.2f})")


_MODEL = None


def _model():
    """Shared embedding model (same one as Sort by category)."""
    global _MODEL
    import numpy as np
    from fastembed import TextEmbedding

    from .categories import MODEL_CACHE, MODEL_NAME

    if _MODEL is None:
        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        _MODEL = TextEmbedding(MODEL_NAME, cache_dir=str(MODEL_CACHE))

    def norm(v):
        v = np.asarray(v, dtype="float32")
        return v / (np.linalg.norm(v) or 1.0)

    return _MODEL, norm
