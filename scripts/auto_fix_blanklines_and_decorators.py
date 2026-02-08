#!/usr/bin/env python3
"""
Auto-fix blank-line issues reported by flake8:
 - Collapse sequences of 3+ blank lines to 2
 - Remove blank lines between decorators and the following def/class
 - Ensure file ends with a single newline

Backups are written as .bak files before changes.
"""

import os
from typing import List

ROOT_EXCLUDE = {".venv", "venv", "__pycache__", ".git", "logs", "data", "docs", "run"}


def should_skip(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(p in ROOT_EXCLUDE for p in parts)


def fix_blanklines_and_decorators(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Collapse 3+ blank lines to 2
    out: List[str] = []
    blank_run = 0
    for ln in lines:
        if ln == "":
            blank_run += 1
        else:
            if blank_run > 2:
                out.extend(["", ""])
            elif blank_run > 0:
                out.extend([""] * blank_run)
            blank_run = 0
            out.append(ln)
    if blank_run > 0:
        if blank_run > 2:
            out.extend(["", ""])
        else:
            out.extend([""] * blank_run)

    # Remove blank lines between decorator(s) and def/class
    final: List[str] = []
    i = 0
    n = len(out)
    while i < n:
        ln = out[i]
        final.append(ln)
        if ln.strip().startswith("@"):
            # peek ahead: remove intermediate blank lines until next non-blank
            j = i + 1
            while j < n and out[j] == "":
                j += 1
            if j < n and (out[j].lstrip().startswith("def ") or out[j].lstrip().startswith("class ")):
                # remove blank lines between i and j
                # so we need to skip adding the blank lines
                # advance i to j-1 so next loop will append the def/class line
                i = j - 1
        i += 1

    result = "\n".join(final)
    # Ensure single trailing newline
    if not result.endswith("\n"):
        result += "\n"
    return result


def process_file(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        orig = f.read()

    new = fix_blanklines_and_decorators(orig)
    if new != orig:
        bak = path + ".bak"
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                f.write(orig)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        return True
    return False


def main():
    repo_root = os.getcwd()
    changed = []
    for root, dirs, files in os.walk(repo_root):
        rel = os.path.relpath(root, repo_root)
        if rel == ".":
            parts = []
        else:
            parts = rel.split(os.sep)
        if any(p in ROOT_EXCLUDE for p in parts):
            continue

        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            if should_skip(path):
                continue
            try:
                ok = process_file(path)
            except Exception as e:
                print(f"ERROR {path}: {e}")
                continue
            if ok:
                changed.append(path)

    if changed:
        print("Modified files:")
        for p in changed:
            print(" -", p)
    else:
        print("No changes made.")


if __name__ == "__main__":
    main()
