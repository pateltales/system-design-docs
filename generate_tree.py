#!/usr/bin/env python3
"""Run this script whenever you add/remove markdown files to regenerate tree.json."""
import json
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
SKIP = {"node_modules", "public", ".git", "__pycache__"}


def build_tree(directory: Path) -> dict:
    result = {"files": [], "dirs": []}
    try:
        items = sorted(directory.iterdir(), key=lambda p: p.name)
    except PermissionError:
        return result

    for item in items:
        if item.name.startswith(".") or item.name in SKIP:
            continue
        if item.is_dir():
            sub = build_tree(item)
            if sub["files"] or sub["dirs"]:
                result["dirs"].append({"name": item.name, **sub})
        elif item.suffix == ".md":
            result["files"].append({
                "name": item.stem,
                "path": str(item.relative_to(ROOT))
            })

    return result


tree = build_tree(ROOT)
out = ROOT / "tree.json"
out.write_text(json.dumps(tree, indent=2))
print(f"✓ Generated tree.json ({len(out.read_text())} bytes)")
