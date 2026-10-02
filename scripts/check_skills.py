#!/usr/bin/env python3
"""Structure lint for backend-skills. Exit 0 when clean, 1 with one error per line."""
import json
import sys
from pathlib import Path

REQUIRED_SKILLS = [
    "idempotency",
    "transaction-boundaries",
    "timeouts-and-retries",
    "safe-migrations",
    "money-handling",
]
REQUIRED_HEADINGS = [
    "## When to use",
    "## Checklist",
    "## Bad example",
    "## What to flag",
    "## Good example",
]
PLACEHOLDERS = ("TODO", "TBD", "FIXME")
MAX_DESCRIPTION = 400
TEXT_SUFFIXES = {".md", ".json", ".py", ".sh", ".yml", ".yaml", ".txt", ".signal"}


def parse_frontmatter(text):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    fields = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def check_skill(skill_dir):
    name = skill_dir.name
    path = skill_dir / "SKILL.md"
    if not path.exists():
        return [f"{name}: SKILL.md not found"]
    text = path.read_text()
    errors = []
    fm = parse_frontmatter(text)
    if fm is None:
        return [f"{name}: missing frontmatter"]
    if fm.get("name") != name:
        errors.append(f"{name}: frontmatter name '{fm.get('name')}' does not match directory")
    desc = fm.get("description", "")
    if not desc.startswith("Use when"):
        errors.append(f"{name}: description must start with 'Use when'")
    if len(desc) > MAX_DESCRIPTION:
        errors.append(f"{name}: description longer than {MAX_DESCRIPTION} characters")
    if ": " in desc or " #" in desc:
        errors.append(f"{name}: description must not contain ': ' or ' #'")
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            errors.append(f"{name}: missing heading '{heading}'")
    if text.count("```") < 4:
        errors.append(f"{name}: needs at least 2 fenced code blocks")
    for token in PLACEHOLDERS:
        if token in text:
            errors.append(f"{name}: contains placeholder '{token}'")
    return errors


def check_skills(root):
    errors = []
    skills_dir = root / "skills"
    present = {p.name for p in skills_dir.iterdir() if p.is_dir()} if skills_dir.exists() else set()
    for name in REQUIRED_SKILLS:
        if name not in present:
            errors.append(f"missing skill: {name}")
    for name in sorted(present):
        errors.extend(check_skill(skills_dir / name))
    return errors


def check_manifests(root):
    errors = []
    plugin = root / ".claude-plugin" / "plugin.json"
    market = root / ".claude-plugin" / "marketplace.json"
    if not plugin.exists():
        errors.append("plugin.json: not found")
    else:
        if json.loads(plugin.read_text()).get("name") != "backend-skills":
            errors.append("plugin.json: name must be 'backend-skills'")
    if not market.exists():
        errors.append("marketplace.json: not found")
    else:
        plugins = json.loads(market.read_text()).get("plugins", [])
        if not plugins or plugins[0].get("source") != "./":
            errors.append("marketplace.json: plugins[0].source must be './'")
    return errors


def check_banned(root):
    banned_file = root / ".local-banned.txt"
    if not banned_file.exists():
        return []
    terms = [t.strip().lower() for t in banned_file.read_text().splitlines() if t.strip()]
    errors = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or not path.is_file() or path.name == ".local-banned.txt":
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {"LICENSE", ".gitignore"}:
            continue
        content = path.read_text(errors="ignore").lower()
        for term in terms:
            if term in content:
                errors.append(f"{path.relative_to(root)}: banned term '{term}'")
    return errors


def check_snippet(root):
    path = root / "snippets" / "CLAUDE.md"
    if not path.exists():
        return ["snippets/CLAUDE.md: not found"]
    text = path.read_text()
    return [f"snippets/CLAUDE.md: does not mention '{name}'"
            for name in REQUIRED_SKILLS if name not in text]


def check(root):
    root = Path(root)
    return check_manifests(root) + check_skills(root) + check_snippet(root) + check_banned(root)


def main():
    root = Path(__file__).resolve().parent.parent
    errors = check(root)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
