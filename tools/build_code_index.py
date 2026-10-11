from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SOURCE_FILES = [ROOT / "text_to_mp3.py", *sorted((ROOT / "tts_app").glob("*.py"))]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def collect() -> tuple[list[dict], list[dict]]:
    modules: list[dict] = []
    symbols: list[dict] = []

    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        module = {
            "path": rel(path),
            "bytes": path.stat().st_size,
            "lines": text.count("\n") + 1,
        }
        modules.append(module)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    {
                        "symbol": node.name,
                        "kind": "function",
                        "path": rel(path),
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    }
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(
                    {
                        "symbol": node.name,
                        "kind": "class",
                        "path": rel(path),
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    }
                )
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            {
                                "symbol": f"{node.name}.{child.name}",
                                "short_symbol": child.name,
                                "kind": "method",
                                "path": rel(path),
                                "line": child.lineno,
                                "end_line": child.end_lineno,
                            }
                        )

    return modules, symbols


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    modules, symbols = collect()
    payload = {"schema": 1, "modules": modules, "symbols": symbols}
    (DOCS / "SYMBOL_INDEX.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (DOCS / "MODULE_SIZES.json").write_text(
        json.dumps(
            {"schema": 2, "modules": sorted(modules, key=lambda item: item["bytes"], reverse=True)},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    rows = [
        "# SYMBOL INDEX — быстрый индекс для Codex",
        "",
        "Сгенерировано `python tools/build_code_index.py`. Для точечного поиска удобнее `python tools/where.py <имя>`.",
        "",
        "| Symbol | Kind | File | Lines |",
        "|---|---|---|---:|",
    ]
    for item in sorted(symbols, key=lambda x: (x["path"], x["line"])):
        rows.append(
            f'| `{item["symbol"]}` | {item["kind"]} | `{item["path"]}` | {item["line"]}–{item["end_line"]} |'
        )
    (DOCS / "SYMBOL_INDEX.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
