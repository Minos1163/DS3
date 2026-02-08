#!/usr/bin/env python3
"""
Auto-fix f-strings without placeholders and remove unused top-level imports.

This script will:
 - Convert "..."/'...' strings that contain no '{' to regular strings.
 - Remove unused top-level imports (imports where none of the imported
   names are referenced elsewhere in the module). For ImportFrom, will
   keep used names and remove unused ones; if all names unused, remove
   the whole import statement.

Backups are written as .bak files before changes.
"""

import ast
import os
import re
from typing import List, Set, Tuple

ROOT_EXCLUDE = {".venv", "venv", "__pycache__", ".git", "logs", "data", "docs", "run"}


def should_skip(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(p in ROOT_EXCLUDE for p in parts)


FSTRING_RE = re.compile(r"(?P<prefix>[fF])(?P<quote>(?:'''|\"\"\"|'|\"))(?P<body>.*?)(?P=quote)", re.S)


def demote_plain_fstrings(source: str) -> str:
    # Replace f-strings that don't contain { or } with plain strings
    def repl(m: re.Match) -> str:
        body = m.group("body")
        # if there are format braces, leave as-is
        if "{" in body or "}" in body:
            return m.group(0)
        # preserve quotes exactly
        quote = m.group("quote")
        return f"{quote}{body}{quote}"

    return FSTRING_RE.sub(repl, source)


def analyze_used_names(tree: ast.AST) -> Set[str]:
    used: Set[str] = set()

    class NameVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name):
            used.add(node.id)

    NameVisitor().visit(tree)
    return used


def remove_unused_imports(source: str) -> str:
    try:
        tree = ast.parse(source)
    except Exception:
        return source

    used = analyze_used_names(tree)

    lines = source.splitlines()
    edits: List[Tuple[int, int, str]] = []  # (start, end, replacement)

    for node in tree.body:
        if isinstance(node, ast.Import):
            # collect imported names
            names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
            if not any(n in used for n in names):
                s = node.lineno
                e = getattr(node, "end_lineno", node.lineno)
                edits.append((s, e, ""))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            names = [alias.asname or alias.name for alias in node.names]
            used_names = [n for n in names if n in used]
            s = node.lineno
            e = getattr(node, "end_lineno", node.lineno)
            if not used_names:
                edits.append((s, e, ""))
            elif len(used_names) < len(names):
                # build replacement import line keeping only used names
                module = node.module or ""
                new_line = f"from {module} import {', '.join(used_names)}"
                edits.append((s, e, new_line))

    if not edits:
        return source

    # apply edits from bottom to top
    edits.sort(reverse=True, key=lambda x: x[0])
    for s, e, rep in edits:
        # convert to 0-based indices
        start_idx = s - 1
        end_idx = e
        # replace those lines
        lines[start_idx:end_idx] = [rep] if rep != "" else []

    # cleanup: remove accidental multiple blank lines
    out = "\n".join(lines)
    out = re.sub("\n{3,}", "\n\n", out)
    return out


def process_file(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    orig = src

    src = demote_plain_fstrings(src)
    src = remove_unused_imports(src)

    if src != orig:
        bak = path + ".bak"
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                f.write(orig)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
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
                print(f"ERROR processing {path}: {e}")
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
