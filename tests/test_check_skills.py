import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_skills  # noqa: E402

VALID = """---
name: {name}
description: Use when testing the linter with a valid skill
---

# {name}

## When to use
- always

## Checklist
1. one

## Bad example
```java
bad();
```

## What to flag
- the bad call

## Good example
```java
good();
```
"""


def make_repo(root: Path, skills=None):
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "backend-skills", "version": "0.1.0"}))
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "backend-skills",
                    "plugins": [{"name": "backend-skills", "source": "./"}]}))
    for name in (check_skills.REQUIRED_SKILLS if skills is None else skills):
        write_skill(root, name, VALID.format(name=name))
    (root / "snippets").mkdir(exist_ok=True)
    (root / "snippets" / "CLAUDE.md").write_text(
        "# Backend rules\n" + "\n".join(f"- {n}" for n in check_skills.REQUIRED_SKILLS) + "\n")


def write_skill(root: Path, name: str, text: str):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text)


class CheckSkillsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def errors(self):
        return check_skills.check(self.root)

    def test_valid_repo_is_clean(self):
        make_repo(self.root)
        self.assertEqual(self.errors(), [])

    def test_missing_required_skill(self):
        make_repo(self.root, skills=["idempotency"])
        self.assertIn("missing skill: money-handling", self.errors())

    def test_name_must_match_directory(self):
        make_repo(self.root)
        write_skill(self.root, "idempotency", VALID.format(name="other"))
        self.assertIn("idempotency: frontmatter name 'other' does not match directory", self.errors())

    def test_description_must_start_with_use_when(self):
        make_repo(self.root)
        write_skill(self.root, "idempotency",
                    VALID.format(name="idempotency").replace("Use when testing", "Helps with testing"))
        self.assertIn("idempotency: description must start with 'Use when'", self.errors())

    def test_description_colon_space_rejected(self):
        make_repo(self.root)
        write_skill(self.root, "idempotency",
                    VALID.format(name="idempotency").replace("the linter", "the linter: really"))
        self.assertIn("idempotency: description must not contain ': ' or ' #'", self.errors())

    def test_description_too_long(self):
        make_repo(self.root)
        long_desc = "Use when " + "x" * 420
        text = VALID.format(name="idempotency").replace(
            "Use when testing the linter with a valid skill", long_desc)
        write_skill(self.root, "idempotency", text)
        self.assertIn("idempotency: description longer than 400 characters", self.errors())

    def test_missing_heading(self):
        make_repo(self.root)
        write_skill(self.root, "idempotency",
                    VALID.format(name="idempotency").replace("## What to flag", "## Notes"))
        self.assertIn("idempotency: missing heading '## What to flag'", self.errors())

    def test_needs_two_code_blocks(self):
        make_repo(self.root)
        text = VALID.format(name="idempotency").replace("```java\ngood();\n```\n", "good();\n")
        write_skill(self.root, "idempotency", text)
        self.assertIn("idempotency: needs at least 2 fenced code blocks", self.errors())

    def test_placeholder_rejected(self):
        make_repo(self.root)
        write_skill(self.root, "idempotency",
                    VALID.format(name="idempotency") + "\nTODO finish\n")
        self.assertIn("idempotency: contains placeholder 'TODO'", self.errors())

    def test_banned_term_from_local_file(self):
        make_repo(self.root)
        (self.root / ".local-banned.txt").write_text("acme-secret\n")
        write_skill(self.root, "idempotency",
                    VALID.format(name="idempotency") + "\nsee Acme-Secret docs\n")
        errs = self.errors()
        self.assertTrue(any("banned term 'acme-secret'" in e for e in errs), errs)

    def test_manifest_name_mismatch(self):
        make_repo(self.root)
        (self.root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x"}))
        self.assertIn("plugin.json: name must be 'backend-skills'", self.errors())

    def test_marketplace_must_point_at_repo_root(self):
        make_repo(self.root)
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "backend-skills", "plugins": [{"name": "backend-skills", "source": "./sub"}]}))
        self.assertIn("marketplace.json: plugins[0].source must be './'", self.errors())

    def test_snippet_must_mention_every_skill(self):
        make_repo(self.root)
        (self.root / "snippets" / "CLAUDE.md").write_text("# Backend rules\n- idempotency\n")
        self.assertIn("snippets/CLAUDE.md: does not mention 'money-handling'", self.errors())


if __name__ == "__main__":
    unittest.main()
