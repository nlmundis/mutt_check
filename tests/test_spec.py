"""Tests for how mutcheck reads a spec: what it accepts and what it refuses.

Everything here is decided at load time, before any sandbox exists, so a
mistake in the spec is a one-line error with exit 2 rather than a run that
quietly checks something other than what was written.
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path

import mutcheck
from tests.test_mutcheck import MUTANT_LOWER, SLUGIFY, MutcheckCase, run_main, toml_mutant

RUN = '[run]\nsuites = ["tests.test_slugify"]\n'
MUTANT_STRIP = ("strip_dropped", "slugify.py", 'return text.strip("-")', "return text")


class SpecCase(MutcheckCase):
    def assert_rejected(self, pattern: str, spec_text: str) -> None:
        self.fx.spec_path.write_text(spec_text)
        with self.assertRaisesRegex(mutcheck.SpecError, pattern):
            mutcheck.load_spec(self.fx.spec_path)

    def external_hook(self) -> Path:
        hook = Path(self._tmp.name) / "deployed" / "hook.py"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(SLUGIFY)
        return hook


class UnknownKeyTest(SpecCase):
    """A misspelled key is an error, never a setting silently dropped."""

    def test_top_level(self):
        self.assert_rejected(r"the spec: unknown key\(s\) mutants",
                             "mutants = 1\n" + RUN + toml_mutant(*MUTANT_LOWER))

    def test_run_table(self):
        self.assert_rejected(r"\[run\]: unknown key\(s\) allow_skip; known: ",
                             RUN + "allow_skip = true\n" + toml_mutant(*MUTANT_LOWER))

    def test_stage_table(self):
        hook = self.external_hook()
        self.assert_rejected(
            r"\[stage\]: unknown key\(s\) alias",
            RUN + f'[stage]\nfile = "{hook}"\nenv = "HOOK"\nalias = "x"\n'
            + toml_mutant("m", None, "text = text.lower()", "text = text"))

    def test_mutant_table(self):
        self.assert_rejected(re.escape("mutant 'lowercase_dropped': unknown key(s) suite"),
                             RUN + toml_mutant(*MUTANT_LOWER, suite='["x"]'))

    def test_edit_table(self):
        self.assert_rejected(re.escape("mutant 'm' edit #1: unknown key(s) relpace"),
                             RUN + textwrap.dedent('''\
            [[mutant]]
            name = "m"
            [[mutant.edit]]
            file = "slugify.py"
            find = "text = text.lower()"
            replace = "text = text"
            relpace = "typo"
            '''))


class SpecLoadTest(SpecCase):
    def test_spec_path_that_is_a_directory_exits_two(self):
        code, out, err = run_main(str(self.fx.root))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE)
        self.assertIn("cannot read spec", err)
        self.assertEqual(out, "")

    def test_spec_that_is_not_utf8_exits_two(self):
        self.fx.spec_path.write_bytes(b"[run]\nsuites = ['\xff']\n")
        code, _, err = run_main(str(self.fx.spec_path))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE)
        self.assertIn("cannot read spec", err)

    def test_symlinked_spec_keeps_the_directory_it_was_named_in(self):
        elsewhere = Path(self._tmp.name) / "specs"
        elsewhere.mkdir()
        real = elsewhere / "mutcheck.toml"
        real.write_text(RUN + toml_mutant(*MUTANT_LOWER))
        link = self.fx.root / "linked.toml"
        os.symlink(real, link)
        self.assertEqual(mutcheck.load_spec(link).root, self.fx.root.resolve())

    def test_mutant_with_no_edit_says_so(self):
        self.assert_rejected("needs file/find/replace", RUN + '[[mutant]]\nname = "m"\n')

    def test_edit_that_is_not_a_list_says_so(self):
        self.assert_rejected("must be a list", RUN + '[[mutant]]\nname = "m"\nedit = "x"\n')

    def test_edit_list_that_is_empty_says_so(self):
        self.assert_rejected("at least one edit", RUN + '[[mutant]]\nname = "m"\nedit = []\n')

    def test_edit_paths_are_normalised(self):
        self.fx.spec_path.write_text(
            RUN + toml_mutant("m", "./slugify.py", "text = text.lower()", "text = text"))
        edit = mutcheck.load_spec(self.fx.spec_path).mutants[0].edits[0]
        self.assertEqual(edit.file, "slugify.py")

    def test_file_naming_a_directory_is_rejected(self):
        self.assert_rejected("not a directory", RUN + toml_mutant("m", "tests/", "a", "b"))

    def test_inline_edit_and_edit_list_together_are_rejected(self):
        self.assert_rejected("not both", RUN + textwrap.dedent('''\
            [[mutant]]
            name = "m"
            file = "slugify.py"
            find = "a"
            replace = "b"
            [[mutant.edit]]
            file = "slugify.py"
            find = "c"
            replace = "d"
            '''))

    def test_run_that_is_not_a_table_is_rejected(self):
        self.assert_rejected(re.escape("[run] must be a table"),
                             "run = 1\n" + toml_mutant(*MUTANT_LOWER))

    def test_booleans_must_be_booleans(self):
        self.assert_rejected("allow_skips must be true or false",
                             RUN + 'allow_skips = "yes"\n' + toml_mutant(*MUTANT_LOWER))
        self.assert_rejected("use_default_ignores must be true or false",
                             RUN + "use_default_ignores = 1\n" + toml_mutant(*MUTANT_LOWER))

    def test_python_must_be_a_non_empty_string(self):
        self.assert_rejected("python must be a non-empty string",
                             RUN + 'python = ""\n' + toml_mutant(*MUTANT_LOWER))

    def test_suites_override_must_not_be_empty(self):
        self.assert_rejected("must not be empty",
                             RUN + toml_mutant(*MUTANT_LOWER, suites="[]"))

    def test_why_and_replace_must_be_strings(self):
        self.assert_rejected("why must be a string",
                             RUN + toml_mutant(*MUTANT_LOWER, why="3"))
        self.assert_rejected("replace must be a string", RUN + textwrap.dedent('''\
            [[mutant]]
            name = "m"
            file = "slugify.py"
            find = "a"
            replace = 3
            '''))


class IgnoreRuleTest(SpecCase):
    def test_ignore_pattern_with_a_separator_is_rejected(self):
        self.assert_rejected("contains a path separator",
                             RUN + 'ignore = ["tests/fixtures"]\n' + toml_mutant(*MUTANT_LOWER))

    def test_edit_under_an_ignored_name_is_rejected(self):
        self.assert_rejected(
            re.escape("scratch/x.py is excluded from the sandbox by ignore pattern 'scratch'"),
            RUN + 'ignore = ["scratch"]\n' + toml_mutant("m", "scratch/x.py", "a", "b"))

    def test_edit_under_a_default_ignored_name_is_rejected(self):
        self.assert_rejected("ignore pattern 'venv'",
                             RUN + toml_mutant("m", "venv/x.py", "a", "b"))


class StageSpecTest(SpecCase):
    def stage_spec(self, hook: Path, extra: str = "") -> str:
        return (RUN + f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\n{extra}'
                + toml_mutant("lowercase_dropped", None, "text = text.lower()", "text = text"))

    def test_as_must_name_a_file_inside_the_temp_directory(self):
        hook = self.external_hook()
        for bad in ("/etc/x", "../x", "sub/../../x", ".", "sub/"):
            with self.subTest(bad=bad):
                self.assert_rejected("`as` must name a file",
                                     self.stage_spec(hook, f'as = "{bad}"\n'))

    def test_env_must_be_a_variable_name(self):
        hook = self.external_hook()
        self.fx.spec_path.write_text(self.stage_spec(hook).replace(
            'env = "HOOK_PATH"', 'env = "HOME=x"'))
        with self.assertRaisesRegex(mutcheck.SpecError, "not a variable name"):
            mutcheck.load_spec(self.fx.spec_path)

    def test_missing_stage_file_is_rejected(self):
        self.assert_rejected("does not exist",
                             self.stage_spec(Path(self._tmp.name) / "nope.py"))

    def test_stage_file_that_is_not_utf8_is_a_spec_error(self):
        hook = self.external_hook()
        hook.write_bytes(b"# caf\xe9\n")
        self.assert_rejected(r"\[stage\] .* is not UTF-8", self.stage_spec(hook))

    def test_stage_file_is_read_once_at_load(self):
        hook = self.external_hook()
        (self.fx.root / "tests" / "test_hook.py").write_text(textwrap.dedent('''\
            import importlib.util, os, unittest

            class HookTest(unittest.TestCase):
                def test_lowercases(self):
                    spec = importlib.util.spec_from_file_location("hook", os.environ["HOOK_PATH"])
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    self.assertEqual(mod.slugify("Hello"), "hello")
            '''))
        self.fx.spec_path.write_text(self.stage_spec(hook).replace(
            "tests.test_slugify", "tests.test_hook"))
        loaded = mutcheck.load_spec(self.fx.spec_path)
        hook.write_text("this is no longer python (\n")
        report = mutcheck.check(loaded)
        self.assertEqual(report.control.outcome, "green", report.control.detail)
        self.assertEqual(report.mutants[0].outcome, "caught")


class ListOnlyTest(SpecCase):
    def test_list_with_only_lists_just_those(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_STRIP)
        code, out, _ = run_main(str(spec), "--list", "--only", "strip_dropped")
        self.assertEqual(code, mutcheck.EXIT_PINNED)
        self.assertIn("strip_dropped", out)
        self.assertNotIn("lowercase_dropped", out)

    def test_list_with_an_unknown_only_name_exits_two(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec), "--list", "--only", "nope")
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE)
        self.assertIn("no such mutant: nope", err)
        self.assertEqual(out, "")


class CommandAndPythonTest(SpecCase):
    def test_python_with_command_is_rejected(self):
        self.assert_rejected(
            "python has no effect with `command`",
            '[run]\ncommand = ["true"]\npython = "python3"\n' + toml_mutant(*MUTANT_LOWER))


class TableShapeTest(SpecCase):
    """Every table check, because without one an invalid spec is a traceback.

    A traceback exits 1, which is the code for a mutant that survived, so an
    unvalidated spec would read as a review finding.
    """

    def raw(self, **run):
        return {"run": {"suites": ["tests.test_slugify"], **run},
                "mutant": [{"name": "m", "file": "slugify.py", "find": "a", "replace": "b"}]}

    def test_stage_that_is_not_a_table(self):
        self.assert_rejected(re.escape("[stage] must be a table"),
                             "stage = 5\n" + RUN + toml_mutant(*MUTANT_LOWER))

    def test_mutant_entry_that_is_not_a_table(self):
        # Before [run], or the key would land inside that table.
        self.assert_rejected(re.escape("[[mutant]] #1 must be a table"),
                             "mutant = [1]\n" + RUN)

    def test_edit_entry_that_is_not_a_table(self):
        self.assert_rejected(re.escape("edit #1 must be a table"),
                             RUN + '[[mutant]]\nname = "m"\nedit = [1]\n')

    def test_root_that_is_not_a_directory(self):
        with self.assertRaisesRegex(mutcheck.SpecError, "project root is not a directory"):
            mutcheck.parse_spec(self.raw(), self.fx.root / "slugify.py")

    def test_python_that_is_not_a_string(self):
        with self.assertRaisesRegex(mutcheck.SpecError, "python must be a non-empty string"):
            mutcheck.parse_spec(self.raw(python=3), self.fx.root)

    def test_env_with_a_null_byte_is_rejected(self):
        hook = self.external_hook()
        raw = self.raw()
        raw["stage"] = {"file": str(hook), "env": "HOOK\0PATH"}
        with self.assertRaisesRegex(mutcheck.SpecError, "not a variable name"):
            mutcheck.parse_spec(raw, self.fx.root)
