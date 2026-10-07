"""Bulk rename: build a plan (old name -> new name) with a preview, then apply it.

Rules, applied in this order: find/replace (text or regular expression), change case, then numbering
with a template ("photo-{n}" -> photo-1, photo-2 …; {n:03} pads; {name} and {ext} are the original
name without extension and the extension). Every new name is checked before anything is renamed:
empty, unchanged, invalid characters, two files ending up with the same name, or a name that is already
taken by another file in the folder.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

BAD_CHARS = set('/\\\0')
WIN_BAD = set('<>:"|?*')


@dataclass
class Rule:
    find: str = ""
    replace: str = ""
    regex: bool = False
    match_case: bool = True
    case: str = ""                 # "" | lower | upper | title
    template: str = ""             # "" = keep the name; e.g. "photo-{n:03}{ext}"
    start: int = 1
    keep_extension: bool = True    # find/replace and case leave the extension alone


@dataclass
class Item:
    old: str
    new: str
    problem: str = ""              # why this one won't be renamed ("" = fine)

    @property
    def changes(self) -> bool:
        return not self.problem and self.new != self.old


def _split(name: str, keep_ext: bool) -> tuple[str, str]:
    if not keep_ext:
        return name, ""
    stem, ext = os.path.splitext(name)
    return (name, "") if not stem else (stem, ext)


def new_name(name: str, rule: Rule, n: int) -> str:
    stem, ext = _split(name, rule.keep_extension)
    if rule.find:
        flags = 0 if rule.match_case else re.IGNORECASE
        pattern = rule.find if rule.regex else re.escape(rule.find)
        replacement = rule.replace if rule.regex else rule.replace.replace("\\", "\\\\")
        stem = re.sub(pattern, replacement, stem, flags=flags)
    if rule.case == "lower":
        stem, ext = stem.lower(), ext.lower()
    elif rule.case == "upper":
        stem, ext = stem.upper(), ext.upper()
    elif rule.case == "title":
        stem = stem.title()
    if rule.template:
        orig_stem, orig_ext = os.path.splitext(name)
        stem = rule.template.format(n=n, name=stem if rule.find or rule.case else orig_stem, ext=ext or orig_ext)
        ext = ""
    return stem + ext


def plan(names: list[str], rule: Rule, existing: set[str] | None = None, windows: bool = False) -> list[Item]:
    """`names` are the files to rename (in the order to number them); `existing` are all names in the folder."""
    existing = set(existing or names)
    items = []
    for i, name in enumerate(names):
        try:
            new = new_name(name, rule, rule.start + i)
        except (re.error, KeyError, IndexError, ValueError) as e:
            items.append(Item(name, name, f"rule error: {e}"))
            continue
        problem = ""
        if not new.strip() or new in (".", ".."):
            problem = "empty name"
        elif any(c in BAD_CHARS for c in new) or windows and any(c in WIN_BAD for c in new):
            problem = "invalid character"
        items.append(Item(name, new, problem))
    renamed = {it.old for it in items if it.changes}
    targets: dict[str, list[Item]] = {}
    for it in items:
        if it.changes:
            targets.setdefault(it.new.lower() if windows else it.new, []).append(it)
    for key, group in targets.items():
        if len(group) > 1:
            for it in group:
                it.problem = "same new name as another file"
        else:
            it = group[0]
            taken = {n.lower() if windows else n for n in existing - renamed}
            if key in taken:
                it.problem = "a file with this name already exists"
    return items


def apply(backend, folder: str, items: list[Item]) -> list[tuple[Item, str]]:
    """Rename on the backend. Names that swap places go through a temporary name first.
    Returns (item, error) for each one that failed."""
    todo = [it for it in items if it.changes]
    olds = {it.old for it in todo}
    failed = []
    staged = []
    for it in todo:                                     # step 1: anything whose target is in use -> temp name
        src = backend.join(folder, it.old)
        if it.new in olds:
            tmp = backend.join(folder, f".{it.old}.blamixfiles-rename")
            try:
                backend.rename(src, tmp)
                staged.append((it, tmp))
            except Exception as e:  # noqa: BLE001
                failed.append((it, str(e)))
        else:
            staged.append((it, src))
    for it, src in staged:                              # step 2: final names
        try:
            backend.rename(src, backend.join(folder, it.new))
        except Exception as e:  # noqa: BLE001
            failed.append((it, str(e)))
    return failed
