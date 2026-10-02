#!/usr/bin/env python3
"""Write the demo results table into README.md between the results markers."""
import re
import sys
from pathlib import Path

START = "<!-- results:start -->"
END = "<!-- results:end -->"
RESULT = re.compile(r"<!-- result: baseline=(\d+) with=(\d+) runs=(\d+) -->")


def _label(stem):
    skill, _, variant = stem.partition(".")
    return f"{skill} ({variant})" if variant else skill


def _order(path):
    skill, _, variant = path.stem.partition(".")
    return (skill, variant != "", variant)


def build_table(demos_dir):
    rows = ["| Skill | Without plugin | With plugin |", "|---|---|---|"]
    for path in sorted(Path(demos_dir).glob("*.md"), key=_order):
        m = RESULT.match(path.read_text().splitlines()[0])
        if not m:
            raise ValueError(f"{path.name}: first line is not a result comment")
        base, with_, runs = m.groups()
        rows.append(f"| `{_label(path.stem)}` | {base} / {runs} | {with_} / {runs} |")
    return "\n".join(rows)


def apply(readme, table):
    if START not in readme or END not in readme:
        raise ValueError("README is missing the results markers")
    head, rest = readme.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n{table}\n{END}{tail}"


def main():
    root = Path(__file__).resolve().parent.parent
    readme_path = root / "README.md"
    table = build_table(root / "docs" / "demos")
    readme_path.write_text(apply(readme_path.read_text(), table))
    print(table)


if __name__ == "__main__":
    sys.exit(main())
