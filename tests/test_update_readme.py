import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_readme  # noqa: E402


class UpdateReadmeTest(unittest.TestCase):
    def test_build_table_reads_result_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "idempotency.md").write_text("<!-- result: baseline=1 with=3 runs=3 -->\n# Demo\n")
            (d / "money-handling.md").write_text("<!-- result: baseline=2 with=2 runs=3 -->\n# Demo\n")
            table = update_readme.build_table(d)
        self.assertIn("| Skill | Without plugin | With plugin |", table)
        self.assertIn("| `idempotency` | 1 / 3 | 3 / 3 |", table)
        self.assertIn("| `money-handling` | 2 / 3 | 2 / 3 |", table)

    def test_variant_is_labelled_and_sorted_after_the_basic_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "idempotency.md").write_text("<!-- result: baseline=3 with=3 runs=3 -->\n")
            (d / "idempotency.hard.md").write_text("<!-- result: baseline=1 with=2 runs=3 -->\n")
            (d / "money-handling.md").write_text("<!-- result: baseline=2 with=2 runs=3 -->\n")
            lines = update_readme.build_table(d).splitlines()
        self.assertEqual(lines[2], "| `idempotency` | 3 / 3 | 3 / 3 |")
        self.assertEqual(lines[3], "| `idempotency (hard)` | 1 / 3 | 2 / 3 |")
        self.assertEqual(lines[4], "| `money-handling` | 2 / 3 | 2 / 3 |")

    def test_apply_replaces_only_between_markers(self):
        readme = "before\n<!-- results:start -->\nold\n<!-- results:end -->\nafter\n"
        out = update_readme.apply(readme, "NEW")
        self.assertEqual(out, "before\n<!-- results:start -->\nNEW\n<!-- results:end -->\nafter\n")

    def test_apply_requires_markers(self):
        with self.assertRaises(ValueError):
            update_readme.apply("no markers here", "NEW")


if __name__ == "__main__":
    unittest.main()
