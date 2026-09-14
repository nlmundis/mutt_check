"""Invariants of the repository itself, which no behaviour test would catch.

Each one here is a fix a later commit could silently undo: a mode bit, a
packaging field, an escape in a README example. The gate has to notice.
"""

from __future__ import annotations

import json
import os
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
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


class AgentsFileTest(unittest.TestCase):
    """The file an agent reads has to name commands that exist."""

    def test_every_make_command_it_names_is_a_real_target(self):
        agents = (ROOT / "AGENTS.md").read_text()
        phony = re.search(r"^\.PHONY: (.+)$", (ROOT / "Makefile").read_text(), re.MULTILINE)
        self.assertIsNotNone(phony, "Makefile declares no .PHONY targets")
        # Commands only: inside a fenced block, or in backticks. Prose such
        # as "make a change here" is not a target.
        blocks = "\n".join(re.findall(r"^```bash\n(.*?)^```", agents,
                                      re.MULTILINE | re.DOTALL))
        named = set(re.findall(r"^make ([a-z][a-z-]*)", blocks, re.MULTILINE))
        named |= set(re.findall(r"`make ([a-z][a-z-]*)`", agents))
        self.assertTrue(named, "AGENTS.md names no make commands")
        assert phony is not None
        self.assertEqual(named - set(phony.group(1).split()), set())

    def test_it_names_the_tool_as_the_command_actually_is(self):
        agents = (ROOT / "AGENTS.md").read_text()
        self.assertIn("mutt_check", agents)
        self.assertNotIn("mutcheck", agents.replace("mutt_check", ""))


class VersionSupportTest(unittest.TestCase):
    """The supported versions are one set, not three that drift apart."""

    def setUp(self):
        self.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text()

    def classified(self):
        listed = "\n".join(self.pyproject["project"]["classifiers"])
        found = re.findall(r"Programming Language :: Python :: (3\.\d+)", listed)
        return sorted(found, key=lambda v: int(v.split(".")[1]))

    def test_the_floor_is_the_oldest_version_claimed(self):
        self.assertEqual(self.pyproject["project"]["requires-python"],
                         f">={self.classified()[0]}")

    def test_ci_runs_every_version_the_package_claims(self):
        matrix = re.search(r"python: \[(.+?)\]", self.workflow)
        self.assertIsNotNone(matrix, "the workflow has no python matrix")
        assert matrix is not None
        tested = sorted(re.findall(r"3\.\d+", matrix.group(1)),
                        key=lambda v: int(v.split(".")[1]))
        self.assertEqual(tested, self.classified())

    def test_the_tomli_fallback_and_its_dependency_agree(self):
        # One without the other means the tool cannot read a spec on the
        # versions it claims, or carries a dependency it never uses.
        fallback = "import tomli as tomllib" in (ROOT / "mutt_check.py").read_text()
        declared = " ".join(self.pyproject["project"]["dependencies"])
        self.assertEqual(fallback, "tomli" in declared and 'python_version < "3.11"' in declared)


class ReleaseAutomationTest(unittest.TestCase):
    """The branch ruleset, the CI job it requires, and the release gate must name each other correctly."""

    def test_the_required_check_is_the_aggregate_job_ci_actually_runs(self):
        ruleset = json.loads((ROOT / ".github" / "rulesets" / "main.json").read_text())
        checks = [rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks"]
        self.assertEqual(len(checks), 1)
        required = {c["context"] for c in checks[0]["parameters"]["required_status_checks"]}
        workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text()
        pattern = r"^  all-checks:\n    name: (.+)\n    if: always\(\)\n    needs: \[check\]$"
        aggregate = re.search(pattern, workflow, re.M)
        assert aggregate is not None, "check.yml has no aggregate all-checks job"
        self.assertEqual(required, {aggregate.group(1)})

    def test_a_release_runs_the_same_gate_and_checks_the_version_first(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("uses: ./.github/workflows/check.yml", release)
        self.assertIn("needs: checks", release)
        self.assertIn("workflow_call:", (ROOT / ".github" / "workflows" / "check.yml").read_text())
        self.assertLess(release.index("mutt_check.__version__"), release.index("gh release create"))
        self.assertNotIn("pypi", release.lower().replace("nothing is uploaded to pypi", ""))

    def test_release_tags_are_protected_from_moving(self):
        ruleset = json.loads((ROOT / ".github" / "rulesets" / "release-tags.json").read_text())
        self.assertEqual(ruleset["target"], "tag")
        self.assertEqual({rule["type"] for rule in ruleset["rules"]}, {"deletion", "non_fast_forward", "update"})

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
            assert row is not None
            self.assertIn(meaning, row.group(1))


if __name__ == "__main__":
    unittest.main()
