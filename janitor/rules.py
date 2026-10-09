"""
Load rules.toml, compile regexes, and match FileInfo against rules.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Iterable, Optional

from .config import DEFAULT_RULES_PATH
from .models import Action, FileInfo, MatchReason, Rule


class RuleSet:
    def __init__(self, rules: list[Rule]):
        # sort by priority, then by name for stability
        self.rules = sorted(rules, key=lambda r: (r.priority, r.name))
        self._compiled: dict[str, re.Pattern] = {}
        for r in self.rules:
            if r.name_regex:
                self._compiled[f"{r.name}:name"] = re.compile(r.name_regex)
            if r.path_regex:
                self._compiled[f"{r.name}:path"] = re.compile(r.path_regex)
            if r.exclude_path_regex:
                self._compiled[f"{r.name}:exclude"] = re.compile(r.exclude_path_regex)

    # ---- matching ----

    def match(self, f: FileInfo) -> Optional[tuple[Rule, MatchReason]]:
        """Return the first matching rule + the reason it matched, or None."""
        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.skip_if_cloud and f.is_cloud:
                continue

            # Exclude path check first — if it matches, this rule doesn't apply.
            if rule.exclude_path_regex:
                pat = self._compiled[f"{rule.name}:exclude"]
                if pat.search(str(f.path)):
                    continue

            if rule.extensions and f.ext not in rule.extensions:
                continue

            if rule.min_size is not None and f.size < rule.min_size:
                continue
            if rule.max_size is not None and f.size > rule.max_size:
                continue

            reason = MatchReason.EXTENSION if rule.extensions else MatchReason.NAME_REGEX

            if rule.name_regex:
                pat = self._compiled[f"{rule.name}:name"]
                if not pat.search(f.name):
                    continue
                reason = MatchReason.NAME_REGEX

            if rule.path_regex:
                pat = self._compiled[f"{rule.name}:path"]
                if not pat.search(str(f.path)):
                    continue
                reason = MatchReason.PATH_REGEX

            return rule, reason

        return None


# ---- loading ----

def _parse_action(s: str) -> Action:
    try:
        return Action(s.lower())
    except ValueError:
        raise ValueError(f"Unknown action: {s!r}. Use 'move', 'trash', or 'skip'.")


def _parse_extensions(raw: Iterable[str]) -> set[str]:
    out = set()
    for e in raw:
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return out


def load_rules(path: Path = DEFAULT_RULES_PATH) -> RuleSet:
    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    raw_rules = data.get("rules", [])
    rules: list[Rule] = []

    for i, r in enumerate(raw_rules):
        name = r.get("name") or f"rule_{i}"
        dest_raw = r.get("destination")
        # Expand ~ and env vars; keep relative paths relative to rules file
        destination = None
        if dest_raw:
            destination = Path(dest_raw).expanduser()

        rules.append(
            Rule(
                name=name,
                priority=int(r.get("priority", 100 + i)),
                destination=destination,
                action=_parse_action(r.get("action", "move")),
                extensions=_parse_extensions(r.get("extensions", [])),
                name_regex=r.get("name_regex"),
                path_regex=r.get("path_regex"),
                min_size=r.get("min_size"),
                max_size=r.get("max_size"),
                exclude_path_regex=r.get("exclude_path_regex"),
                skip_if_project=bool(r.get("skip_if_project", False)),
                skip_if_cloud=bool(r.get("skip_if_cloud", True)),
                preserve_folders=(
                    bool(r["preserve_folders"]) if "preserve_folders" in r else None
                ),
                description=r.get("description", ""),
                enabled=bool(r.get("enabled", True)),
            )
        )

    return RuleSet(rules)