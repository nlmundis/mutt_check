"""Invariants of the repository itself, which no behaviour test would catch.

Each one here is a fix a later commit could silently undo: a mode bit, a
packaging field, an escape in a README example. The gate has to notice.
"""

from __future__ import annotations

import os
import re
import tomllib
import unittest
from pathlib import Path

import mutt_check

ROOT = Path(mutt_check.__file__).resolve().parent
README = (ROOT / "README.md").read_text()


def toml_blocks(markdown: str) -> list[str]:
    """Every fenced ```toml block in ``markdown``, in order."""
    return re.findall(r"^```toml\n(.*?)^```", markdown, re.MULTILINE | re.DOTALL)


class ExecutableBitTest(unittest.TestCase):
    def test_mutt_check_is_executable_so_its_shebang_works(self):
        self.assertTrue(os.access(ROOT / "mutt_check.py", os.X_OK),
                        "mutt_check.py carries a shebang, so it must be executable")


class PackagingTest(unittest.TestCase):
    def setUp(self):
        self.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    def test_license_is_an_spdx_expression_without_the_classifier(self):
        # setuptools deprecated the table form and the classifier together.
        self.assertEqual(self.pyproject["project"]["license"], "MIT")
        self.assertEqual(self.pyproject["project"]["license-files"], ["LICENSE"])
        for classifier in self.pyproject["project"]["classifiers"]:
            self.assertNotIn("License ::", classifier)

    def test_version_has_one_source(self):
        project = self.pyproject["project"]
        self.assertNotIn("version", project)
        self.assertEqual(project["dynamic"], ["version"])
        self.assertEqual(self.pyproject["tool"]["setuptools"]["dynamic"]["version"],
                         {"attr": "mutt_check.__version__"})


class ReadmeExampleTest(unittest.TestCase):
    def test_every_toml_example_parses(self):
        blocks = toml_blocks(README)
        self.assertGreaterEqual(len(blocks), 3, "README lost its spec examples")
        for block in blocks:
            with self.subTest(block=block.splitlines()[0]):
                tomllib.loads(block)

    def test_the_multi_edit_example_uses_literal_backslashes(self):
        # TOML literal strings do not unescape, so a doubled backslash here
        # would be an anchor that matches nothing in the file it names.
        blocks = [b for b in toml_blocks(README) if "mutant.edit" in b]
        self.assertEqual(len(blocks), 1)
        edits = tomllib.loads(blocks[0])["mutant"][0]["edit"]
        self.assertEqual(edits[0]["find"], r"\A\ufeff?---")
        self.assertEqual(edits[0]["replace"], r"\A---")

    def test_documented_exit_codes_are_the_ones_the_code_uses(self):
        for code, meaning in ((mutt_check.EXIT_PINNED, "every mutant caught"),
                              (mutt_check.EXIT_UNPINNED, "survived"),
                              (mutt_check.EXIT_UNUSABLE, "Control red")):
            row = re.search(rf"^\| {code} \| (.+?) \|$", README, re.MULTILINE)
            self.assertIsNotNone(row, f"README has no exit table row for {code}")
            self.assertIn(meaning, row.group(1))


if __name__ == "__main__":
    unittest.main()
