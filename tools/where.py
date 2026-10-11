from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "text_to_mp3.py", *sorted((ROOT / "tts_app").glob("*.py"))]


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/where.py <symbol-or-text>")
        return 2

    query = " ".join(sys.argv[1:]).strip()
    q = query.casefold()
    exact: list[tuple[str, int, int, str]] = []
    text_hits: list[tuple[str, int, str]] = []

    for path in FILES:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if q in node.name.casefold():
                    exact.append((rel, node.lineno, node.end_lineno or node.lineno, node.name))

        if not exact:
            for lineno, line in enumerate(text.splitlines(), 1):
                if q in line.casefold():
                    text_hits.append((rel, lineno, line.strip()))
                    if len(text_hits) >= 20:
                        break

    if exact:
        for rel, start, end, name in sorted(set(exact)):
            print(f"{rel}:{start}-{end}  {name}")
        return 0

    if text_hits:
        for rel, lineno, line in text_hits[:20]:
            print(f"{rel}:{lineno}  {line[:180]}")
        return 0

    print(f"No source match: {query}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
