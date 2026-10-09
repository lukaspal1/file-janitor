"""
User-defined categories, matched by what a file is *about*.

You type a category name and a short description ("Tool catalogues —
product catalogues for cutting tools with part numbers"); a small local
embedding model compares that description to each file's text and name.
Everything runs on this PC — the model (~210 MB) is downloaded once and
cached in ~/.file_janitor/models.

What the model sees per file:
  - PDF / Word / text files: title, file name, page count, and the text
    of the first few pages.
  - Everything else: the file name and its folder name.
"""
from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import APP_DIR, PROJECT_ROOT

DEFAULT_CATEGORIES_PATH = PROJECT_ROOT / "categories.toml"
MODEL_NAME = "BAAI/bge-base-en-v1.5"
MODEL_CACHE = APP_DIR / "models"

# How much of each document to read. The model only looks at ~512 tokens anyway.
MAX_PAGES = 4
MAX_CHARS = 1500

TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".rtf", ".tex", ".log", ".json", ".xml", ".html"}

logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class Category:
    name: str
    description: str = ""
    destination: Optional[Path] = None      # None -> ~/Documents/<name>
    keywords: list[str] = field(default_factory=list)
    extensions: set[str] = field(default_factory=set)   # empty = any file
    min_pages: Optional[int] = None
    max_pages: Optional[int] = None

    @property
    def target(self) -> Path:
        return self.destination or (Path.home() / "Documents" / self.name)

    def prompt(self) -> str:
        parts = [self.name]
        if self.description:
            parts.append(self.description)
        if self.keywords:
            parts.append(", ".join(self.keywords))
        return ". ".join(parts)


@dataclass
class DocSummary:
    """What we know about a file's content."""
    text: str               # what gets embedded
    pages: Optional[int]    # PDFs only
    from_content: bool      # False = file name only


@dataclass
class CategoryMatch:
    category: Category
    score: float
    runner_up: Optional[str]
    from_content: bool
    keyword_hits: list[str]


# ---------- categories.toml ----------

def load_categories(path: Path = DEFAULT_CATEGORIES_PATH) -> list[Category]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out = []
    for c in data.get("categories", []):
        name = (c.get("name") or "").strip()
        if not name:
            continue
        dest = c.get("destination")
        out.append(Category(
            name=name,
            description=c.get("description", ""),
            destination=Path(dest).expanduser() if dest else None,
            keywords=list(c.get("keywords", [])),
            extensions={
                (e if e.startswith(".") else "." + e).lower()
                for e in c.get("extensions", [])
            },
            min_pages=c.get("min_pages"),
            max_pages=c.get("max_pages"),
        ))
    return out


def _toml_str(s: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(s, ensure_ascii=False)


def save_categories(cats: list[Category], path: Path = DEFAULT_CATEGORIES_PATH) -> None:
    lines = [
        "# File Janitor — your categories.",
        "#",
        "# Files are matched by meaning: describe what belongs in each category",
        "# in plain words. Optional fields:",
        "#   destination  — folder to move into (default ~/Documents/<name>)",
        "#   keywords     — words that strongly suggest this category",
        "#   extensions   — only consider these file types",
        "#   min_pages / max_pages — PDFs only",
        "",
    ]
    home = str(Path.home())
    for c in cats:
        lines.append("[[categories]]")
        lines.append(f"name = {_toml_str(c.name)}")
        if c.description:
            lines.append(f"description = {_toml_str(c.description)}")
        if c.destination:
            d = str(c.destination)
            if d.startswith(home):
                d = "~" + d[len(home):].replace("\\", "/")
            lines.append(f"destination = {_toml_str(d)}")
        if c.keywords:
            lines.append("keywords = [" + ", ".join(_toml_str(k) for k in c.keywords) + "]")
        if c.extensions:
            lines.append("extensions = [" + ", ".join(_toml_str(e) for e in sorted(c.extensions)) + "]")
        if c.min_pages is not None:
            lines.append(f"min_pages = {int(c.min_pages)}")
        if c.max_pages is not None:
            lines.append(f"max_pages = {int(c.max_pages)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------- reading files ----------

def _clean_name(path: Path) -> str:
    return re.sub(r"[_\-.]+", " ", path.stem).strip()


def _pdf(path: Path) -> tuple[str, str, Optional[int]]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return "", "", None
    pages = len(reader.pages)
    title = ""
    try:
        meta = reader.metadata
        if meta:
            title = " ".join(x for x in (meta.title, meta.subject) if isinstance(x, str))
    except Exception:
        pass
    chunks, total = [], 0
    for page in reader.pages[:MAX_PAGES]:
        try:
            t = page.extract_text() or ""
        except Exception:
            continue
        chunks.append(t)
        total += len(t)
        if total >= MAX_CHARS:
            break
    return title, " ".join(chunks), pages


def _docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    out, total = [], 0
    for p in doc.paragraphs:
        out.append(p.text)
        total += len(p.text)
        if total >= MAX_CHARS:
            break
    return "\n".join(out)


def summarize(path: Path) -> DocSummary:
    """Build the text the model sees for one file. Never raises."""
    name = _clean_name(path)
    folder = path.parent.name
    ext = path.suffix.lower()
    title, body, pages = "", "", None
    try:
        if ext == ".pdf":
            title, body, pages = _pdf(path)
        elif ext == ".docx":
            body = _docx(path)
        elif ext in TEXT_EXTS:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                body = fh.read(MAX_CHARS)
    except Exception:
        pass

    body = re.sub(r"\s+", " ", body).strip()[:MAX_CHARS]
    header = f"File: {name}. Folder: {folder}."
    if title:
        header = f"Title: {title}. " + header
    if pages:
        header += f" {pages} pages."
    from_content = len(body) >= 200
    return DocSummary(text=f"{header} {body}".strip(), pages=pages, from_content=from_content)


# ---------- matching ----------

class Classifier:
    """
    Scores files against categories with cosine similarity of embeddings,
    plus a small bonus for keyword hits.

    A file is assigned when its best score clears `threshold` AND beats the
    runner-up by `margin` — otherwise it's ambiguous and left alone.
    File-name-only matches have less to go on, so they need a higher score.
    """

    def __init__(self, categories: list[Category], *,
                 threshold: float = 0.50, name_only_threshold: float = 0.55,
                 margin: float = 0.03, keyword_bonus: float = 0.04,
                 model_name: str = MODEL_NAME):
        from fastembed import TextEmbedding

        if not categories:
            raise ValueError("No categories defined.")
        self.categories = categories
        self.threshold = threshold
        self.name_only_threshold = name_only_threshold
        self.margin = margin
        self.keyword_bonus = keyword_bonus

        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        self.model = TextEmbedding(model_name, cache_dir=str(MODEL_CACHE))
        # Categories act as "queries", files as "passages".
        self._cat_vecs = [self._norm(v) for v in self.model.query_embed([c.prompt() for c in categories])]

    @staticmethod
    def _norm(v):
        import numpy as np
        v = np.asarray(v, dtype="float32")
        return v / (np.linalg.norm(v) or 1.0)

    def _eligible(self, cat: Category, path: Path, doc: DocSummary) -> bool:
        if cat.extensions and path.suffix.lower() not in cat.extensions:
            return False
        if cat.min_pages is not None and (doc.pages is None or doc.pages < cat.min_pages):
            return False
        if cat.max_pages is not None and doc.pages is not None and doc.pages > cat.max_pages:
            return False
        return True

    def score_all(self, paths: list[Path], docs: list[DocSummary]) -> list[list[tuple[Category, float, list[str]]]]:
        """Per file: every eligible category with its score, best first."""
        vecs = [self._norm(v) for v in self.model.passage_embed([d.text for d in docs], batch_size=16)]
        out = []
        for path, doc, vec in zip(paths, docs, vecs):
            low = doc.text.lower()
            scored = []
            for cat, cvec in zip(self.categories, self._cat_vecs):
                if not self._eligible(cat, path, doc):
                    continue
                hits = [k for k in cat.keywords if k.lower() in low]
                s = float(vec @ cvec) + self.keyword_bonus * min(len(hits), 3)
                scored.append((cat, s, hits))
            scored.sort(key=lambda t: t[1], reverse=True)
            out.append(scored)
        return out

    def classify(self, paths: list[Path], docs: list[DocSummary]) -> list[Optional[CategoryMatch]]:
        results = []
        for doc, scored in zip(docs, self.score_all(paths, docs)):
            if not scored:
                results.append(None)
                continue
            cat, best, hits = scored[0]
            second = scored[1][1] if len(scored) > 1 else -1.0
            need = self.threshold if doc.from_content else self.name_only_threshold
            if best < need or best - second < self.margin:
                results.append(None)
                continue
            results.append(CategoryMatch(
                category=cat,
                score=best,
                runner_up=scored[1][0].name if len(scored) > 1 else None,
                from_content=doc.from_content,
                keyword_hits=hits,
            ))
        return results


def classify_files(paths: list[Path], categories: list[Category],
                   progress_cb: Optional[Callable[[str, int, int], None]] = None,
                   **classifier_kw) -> list[Optional[CategoryMatch]]:
    """Read every file, then classify. progress_cb(stage, done, total)."""
    docs = []
    for i, p in enumerate(paths, 1):
        if progress_cb:
            progress_cb("Reading files", i, len(paths))
        docs.append(summarize(p))
    if progress_cb:
        progress_cb("Loading model", 0, 0)
    clf = Classifier(categories, **classifier_kw)
    if progress_cb:
        progress_cb("Matching", 0, 0)
    return clf.classify(paths, docs)
