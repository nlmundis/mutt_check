"""Tests for mutt_check, against a small generated project.

The fixture is a slugify module whose suite covers lowercasing and edge
stripping but deliberately NOT the collapsing of repeated separators, so
one mutant survives on purpose. Every test builds its own copy so nothing
leaks between cases.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import mutt_check

SLUGIFY = textwrap.dedent('''\
    import re


    def slugify(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-")
    ''')

SLUGIFY_TESTS = textwrap.dedent('''\
    import unittest

    from slugify import slugify


    class SlugifyTest(unittest.TestCase):
        def test_lowercases(self):
            self.assertEqual(slugify("Hello"), "hello")

        def test_strips_edges(self):
            self.assertEqual(slugify("-hello-"), "hello")
    ''')

MUTANT_LOWER = ("lowercase_dropped", "slugify.py",
                "text = text.lower()", "text = text")
MUTANT_STRIP = ("strip_dropped", "slugify.py",
                'return text.strip("-")', "return text")
MUTANT_COLLAPSE = ("collapse_dropped", "slugify.py",
                   '"[^a-z0-9]+"', '"[^a-z0-9]"')


def toml_mutant(name, file, find, replace, **extra):
    lines = [f"[[mutant]]", f'name = "{name}"']
    if file is not None:
        lines.append(f'file = "{file}"')
    lines += [f"find = '''{find}'''", f"replace = '''{replace}'''"]
    for key, value in extra.items():
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


class Fixture:
    """A throwaway slugify project with a spec, built per test."""

    def __init__(self, tmp: Path):
        self.root = tmp / "proj"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "slugify.py").write_text(SLUGIFY)
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_slugify.py").write_text(SLUGIFY_TESTS)
        (self.root / ".git").mkdir()
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.spec_path = self.root / "mutt_check.toml"

    def write_spec(self, *mutants, run_extra="", top=""):
        body = f'[run]\nsuites = ["tests.test_slugify"]\n{run_extra}\n{top}\n'
        for m in mutants:
            body += toml_mutant(*m) if isinstance(m, tuple) else m
        self.spec_path.write_text(body)
        return self.spec_path

    def digest(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(self.root)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()


def run_main(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mutt_check.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MutcheckCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mutt_check-test-")
        self.fx = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()


class VerdictTest(MutcheckCase):
    def test_pinned_mutants_are_caught_and_exit_zero(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_STRIP)
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)
        self.assertIn("control", out)
        self.assertRegex(out, r"lowercase_dropped\s+caught\s+.*test_lowercases")
        self.assertRegex(out, r"strip_dropped\s+caught\s+.*test_strips_edges")
        self.assertIn("2 of 2 mutants applied, control green, 0 survived, 0 stale, 0 broken",
                      out)

    def test_unpinned_mutant_survives_and_exit_one(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_COLLAPSE)
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"collapse_dropped\s+SURVIVED")
        self.assertIn("1 survived, 0 stale, 0 broken: collapse_dropped", out)

    def test_missing_anchor_is_stale_and_exit_one(self):
        spec = self.fx.write_spec(
            ("gone", "slugify.py", "this text is not there", "x"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"gone\s+STALE\s+anchor appears 0x in slugify.py")
        self.assertIn("0 of 1 mutants applied", out)

    def test_anchor_appearing_twice_is_stale_not_first_match(self):
        spec = self.fx.write_spec(("ambiguous", "slugify.py", "text", "txt"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"ambiguous\s+STALE\s+anchor appears 6x in slugify.py")

    def test_unreadable_target_is_stale(self):
        spec = self.fx.write_spec(("nofile", "missing.py", "a", "b"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"nofile\s+STALE\s+cannot read missing.py")

    def test_red_control_stops_before_any_mutant_and_exits_two(self):
        tests = self.fx.root / "tests" / "test_slugify.py"
        tests.write_text(tests.read_text().replace('"hello")', '"HELLO")', 1))
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertRegex(out, r"(?m)^  control\s+RED\s")
        self.assertNotIn("lowercase_dropped", out)
        self.assertIn("no mutant verdict would mean anything", out)

    def test_skipped_tests_make_the_control_red_by_default(self):
        tests = self.fx.root / "tests" / "test_slugify.py"
        tests.write_text(tests.read_text().replace(
            "    def test_strips_edges",
            '    @unittest.skip("wip")\n    def test_strips_edges', 1))
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertRegex(out, r"control\s+RED\s+1 test\(s\) skipped")

    def test_allow_skips_lets_a_skipping_control_run(self):
        tests = self.fx.root / "tests" / "test_slugify.py"
        tests.write_text(tests.read_text().replace(
            "    def test_strips_edges",
            '    @unittest.skip("wip")\n    def test_strips_edges', 1))
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra="allow_skips = true")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)
        self.assertRegex(out, r"control\s+green\s+2 tests, 1 skipped")

    def test_project_tree_is_never_modified(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_COLLAPSE,
                                  ("gone", "slugify.py", "absent", "x"))
        before = self.fx.digest()
        run_main(str(spec))
        self.assertEqual(self.fx.digest(), before)

    def test_ignored_directories_are_not_copied(self):
        venv = self.fx.root / "venv"
        venv.mkdir()
        (venv / "big").write_text("x" * 10)
        (self.fx.root / "scratch").mkdir()
        (self.fx.root / "scratch" / "note").write_text("n")
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra='ignore = ["scratch"]')
        loaded = mutt_check.load_spec(spec)
        sandbox = mutt_check.run_once(loaded, (), keep=True).sandbox
        try:
            work = sandbox / "project"
            self.assertFalse((work / "venv").exists())
            self.assertFalse((work / "scratch").exists())
            self.assertFalse((work / ".git").exists())
            self.assertTrue((work / "slugify.py").exists())
        finally:
            import shutil
            shutil.rmtree(sandbox, ignore_errors=True)


class SafetyTest(MutcheckCase):
    """Verdicts that must never read as success, and writes that must never land."""

    def test_symlinked_file_is_refused_and_its_target_untouched(self):
        real = Path(self._tmp.name) / "real"
        real.mkdir()
        target = real / "slugify.py"
        target.write_text(SLUGIFY)
        (self.fx.root / "slugify.py").unlink()
        os.symlink(target, self.fx.root / "slugify.py")
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE, out)
        self.assertIn("symlinks in the project point outside it", err)
        self.assertIn("slugify.py ->", err)
        self.assertEqual(target.read_text(), SLUGIFY)

    def test_file_under_a_symlinked_directory_is_refused(self):
        real = Path(self._tmp.name) / "real_pkg"
        real.mkdir()
        (real / "mod.py").write_text("X = 1\n")
        os.symlink(real, self.fx.root / "pkg")
        spec = self.fx.write_spec(("x", "pkg/mod.py", "X = 1", "X = 2"))
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE, out)
        self.assertIn("pkg ->", err)
        self.assertEqual((real / "mod.py").read_text(), "X = 1\n")

    def test_mutant_that_breaks_the_import_is_broken_not_caught(self):
        # Compiles, so only the runner can see it. Given a module by name,
        # unittest wraps an ImportError in a _FailedTest, but lets any other
        # exception raised at import escape, with no summary at all.
        spec = self.fx.write_spec(
            ("raises", "slugify.py", "import re",
             'import re\nraise RuntimeError("mutant raised at import")'))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED, out)
        self.assertRegex(out, r"raises\s+BROKEN\s+RuntimeError: mutant raised at import")
        self.assertIn("1 of 1 mutants applied, control green, 0 survived, 0 stale, "
                      "1 broken: raises", out)

    def test_import_failure_under_discovery_is_broken_too(self):
        # Discovery wraps the failed module in a synthetic _FailedTest and
        # carries on, so the run has a "Ran N tests" line and an ERROR: entry
        # that would otherwise read as caught.
        spec = self.fx.write_spec(
            ("missing", "slugify.py", "import re", "import re_mutt_check_missing"),
            run_extra=(f'command = ["{sys.executable}", "-B", "-m", "unittest", '
                       '"discover", "-s", "tests", "-t", "."]'))
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED, out)
        # The root cause inside the _FailedTest block, not unittest's
        # generic "Failed to import test module" wrapper line.
        self.assertRegex(out, r"missing\s+BROKEN\s+ModuleNotFoundError")

    def test_suite_that_runs_no_tests_makes_the_control_red(self):
        (self.fx.root / "tests" / "test_empty.py").write_text("import unittest\n")
        spec = self.fx.write_spec(MUTANT_LOWER)
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]', 'suites = ["tests.test_empty"]', 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertRegex(out, r"control\s+RED\s+0 tests ran")

    def test_hanging_mutant_is_broken_after_the_timeout(self):
        spec = self.fx.write_spec(
            ("spin", "slugify.py", "text = text.lower()",
             "text = text.lower()\n    while True: pass"),
            run_extra="timeout = 2")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED, out)
        self.assertRegex(out, r"spin\s+BROKEN\s+timed out after 2s")

    def test_non_utf8_target_is_stale_with_its_own_reason(self):
        (self.fx.root / "latin.py").write_bytes(b"# caf\xe9\nX = 1\n")
        spec = self.fx.write_spec(("enc", "latin.py", "X = 1", "X = 2"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"enc\s+STALE\s+latin.py is not UTF-8")

    def test_line_endings_survive_an_edit(self):
        import shutil
        crlf = SLUGIFY.replace("\n", "\r\n")
        (self.fx.root / "slugify.py").write_bytes(crlf.encode())
        spec = self.fx.write_spec(MUTANT_LOWER)
        loaded = mutt_check.load_spec(spec)
        result = mutt_check.run_once(loaded, loaded.mutants[0].edits, keep=True)
        try:
            copied = (result.sandbox / "project" / "slugify.py").read_bytes()
            self.assertIn(b"\r\n", copied)
            self.assertNotIn(b"text = text.lower()", copied)
            self.assertEqual(copied.count(b"\n"), copied.count(b"\r\n"))
        finally:
            shutil.rmtree(result.sandbox, ignore_errors=True)

    def test_use_default_ignores_false_copies_the_git_dir(self):
        import shutil
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra="use_default_ignores = false")
        loaded = mutt_check.load_spec(spec)
        result = mutt_check.run_once(loaded, (), keep=True)
        try:
            self.assertTrue((result.sandbox / "project" / ".git" / "HEAD").is_file())
        finally:
            shutil.rmtree(result.sandbox, ignore_errors=True)


class EditTest(MutcheckCase):
    def test_multi_edit_mutant_applies_every_edit(self):
        # Each edit alone is caught; together they must still be caught, and
        # the verdict names both failing tests.
        spec = self.fx.write_spec(textwrap.dedent('''\
            [[mutant]]
            name = "both"
            [[mutant.edit]]
            file = "slugify.py"
            find = "text = text.lower()"
            replace = "text = text"
            [[mutant.edit]]
            file = "slugify.py"
            find = 'return text.strip("-")'
            replace = "return text"
            '''))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)
        self.assertRegex(out, r"both\s+caught\s+.*test_lowercases.*test_strips_edges")

    def test_multi_edit_with_one_stale_anchor_is_stale_as_a_whole(self):
        spec = self.fx.write_spec(textwrap.dedent('''\
            [[mutant]]
            name = "half"
            [[mutant.edit]]
            file = "slugify.py"
            find = "text = text.lower()"
            replace = "text = text"
            [[mutant.edit]]
            file = "slugify.py"
            find = "not present"
            replace = ""
            '''))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"half\s+STALE")

    def test_apply_edits_writes_nothing_when_a_later_anchor_is_stale(self):
        written = {}
        edits = (mutt_check.Edit("a.py", "x", "y"), mutt_check.Edit("a.py", "zz", "q"))
        reason = mutt_check.apply_edits(
            edits, lambda _f: "x and more", lambda f, t: written.__setitem__(f, t))
        self.assertIn("appears 0x", reason)
        self.assertEqual(written, {})

    def test_apply_edits_reports_an_unreadable_target(self):
        def read(_f):
            raise mutt_check._Unreadable("nope")
        reason = mutt_check.apply_edits(
            (mutt_check.Edit("a.py", "x", "y"),), read, lambda f, t: None)
        self.assertEqual(reason, "nope")

    def test_second_edit_sees_the_first_edits_result(self):
        written = {}
        edits = (mutt_check.Edit("a.py", "x", "y"), mutt_check.Edit("a.py", "y", "z"))
        reason = mutt_check.apply_edits(
            edits, lambda _f: "x", lambda f, t: written.__setitem__(f, t))
        self.assertIsNone(reason)
        self.assertEqual(written, {"a.py": "z"})

    def test_per_mutant_suites_override_decides_the_verdict(self):
        # A second suite that never imports slugify cannot catch anything, so
        # a mutant judged against it alone must survive.
        (self.fx.root / "tests" / "test_other.py").write_text(textwrap.dedent('''\
            import unittest

            class OtherTest(unittest.TestCase):
                def test_nothing(self):
                    self.assertTrue(True)
            '''))
        spec = self.fx.write_spec(
            toml_mutant(*MUTANT_LOWER, suites='["tests.test_other"]'))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertRegex(out, r"lowercase_dropped\s+SURVIVED")


class StageTest(MutcheckCase):
    """The suite reads the target through an env var, not from the tree."""

    def _external_hook(self) -> Path:
        hook = Path(self._tmp.name) / "deployed" / "hook.py"
        hook.parent.mkdir()
        hook.write_text(SLUGIFY)
        return hook

    def _suite_reading_env(self, env: str, relative: str | None):
        loader = (f"Path(os.environ['{env}'])" if relative is None
                  else f"Path(os.environ['{env}']) / '{relative}'")
        (self.fx.root / "tests" / "test_hook.py").write_text(textwrap.dedent(f'''\
            import importlib.util, os, unittest
            from pathlib import Path

            def load():
                path = {loader}
                spec = importlib.util.spec_from_file_location("hook", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod

            class HookTest(unittest.TestCase):
                def test_lowercases(self):
                    self.assertEqual(load().slugify("Hello"), "hello")
            '''))

    def test_stage_env_names_the_staged_file(self):
        hook = self._external_hook()
        self._suite_reading_env("HOOK_PATH", None)
        spec = self.fx.write_spec(
            toml_mutant("lowercase_dropped", None, "text = text.lower()", "text = text"),
            toml_mutant("strip_dropped", None, 'return text.strip("-")', "return text"),
            run_extra='suites = ["tests.test_hook"]',
            top=f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\n')
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED, out)
        self.assertRegex(out, r"lowercase_dropped\s+caught")
        self.assertRegex(out, r"strip_dropped\s+SURVIVED")
        self.assertEqual(hook.read_text(), SLUGIFY)

    def test_stage_as_puts_the_file_under_a_temp_root(self):
        hook = self._external_hook()
        self._suite_reading_env("HOME", ".claude/hooks/hook.py")
        spec = self.fx.write_spec(
            toml_mutant("lowercase_dropped", None, "text = text.lower()", "text = text"),
            top=f'[stage]\nfile = "{hook}"\nenv = "HOME"\nas = ".claude/hooks/hook.py"\n')
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]', 'suites = ["tests.test_hook"]', 1))
        home_before = os.environ.get("HOME")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)
        self.assertEqual(os.environ.get("HOME"), home_before)
        self.assertEqual(hook.read_text(), SLUGIFY)

    def test_stage_mode_rejects_a_file_on_an_edit(self):
        hook = self._external_hook()
        spec = self.fx.write_spec(
            MUTANT_LOWER, top=f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\n')
        with self.assertRaisesRegex(mutt_check.SpecError, "drop `file`"):
            mutt_check.load_spec(spec)


class CommandTest(MutcheckCase):
    def test_custom_command_replaces_the_unittest_runner(self):
        checker = self.fx.root / "check.py"
        checker.write_text(textwrap.dedent('''\
            import sys
            from slugify import slugify
            sys.exit(0 if slugify("Hello") == "hello" else 1)
            '''))
        spec = self.fx.write_spec(
            MUTANT_LOWER, MUTANT_STRIP,
            run_extra=f'command = ["{sys.executable}", "check.py"]')
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED, out)
        self.assertRegex(out, r"lowercase_dropped\s+caught")
        self.assertRegex(out, r"strip_dropped\s+SURVIVED")

    def test_command_and_suites_together_are_rejected(self):
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra='command = ["true"]')
        with self.assertRaisesRegex(mutt_check.SpecError, "not both"):
            mutt_check.load_spec(spec)

    def test_per_mutant_suites_need_the_default_runner(self):
        spec = self.fx.write_spec(
            toml_mutant(*MUTANT_LOWER, suites='["tests.test_slugify"]'),
            run_extra='command = ["true"]')
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        with self.assertRaisesRegex(mutt_check.SpecError, "per-mutant"):
            mutt_check.load_spec(spec)


class SpecValidationTest(MutcheckCase):
    def assert_rejected(self, pattern, *mutants, **kw):
        spec = self.fx.write_spec(*mutants, **kw)
        with self.assertRaisesRegex(mutt_check.SpecError, pattern):
            mutt_check.load_spec(spec)

    def test_no_op_mutant_is_rejected(self):
        self.assert_rejected("no-op", ("same", "slugify.py", "x", "x"))

    def test_duplicate_names_are_rejected(self):
        self.assert_rejected("duplicate", MUTANT_LOWER, MUTANT_LOWER)

    def test_reserved_control_name_is_rejected(self):
        self.assert_rejected("reserved", ("control", "slugify.py", "a", "b"))

    def test_absolute_or_escaping_paths_are_rejected(self):
        self.assert_rejected("relative", ("abs", "/etc/passwd", "a", "b"))
        self.assert_rejected("relative", ("up", "../x.py", "a", "b"))

    def test_empty_find_is_rejected(self):
        self.assert_rejected("non-empty", ("empty", "slugify.py", "", "b"))

    def test_missing_mutants_are_rejected(self):
        self.assert_rejected(r"no \[\[mutant\]\]")

    def test_missing_runner_is_rejected(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        with self.assertRaisesRegex(mutt_check.SpecError, "needs `suites`"):
            mutt_check.load_spec(spec)

    def test_missing_spec_file_exits_two(self):
        code, _, err = run_main(str(self.fx.root / "nope.toml"))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertIn("no spec at", err)

    def test_malformed_toml_exits_two(self):
        self.fx.spec_path.write_text("[run\n")
        code, _, err = run_main(str(self.fx.spec_path))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertIn("mutt_check:", err)

    def test_relative_python_resolves_against_the_root(self):
        venv_python = self.fx.root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra='python = "venv/bin/python"')
        loaded = mutt_check.load_spec(spec)
        self.assertEqual(loaded.python,
                         str(self.fx.root.resolve() / "venv/bin/python"))
        name = Path(sys.executable).name
        path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path}):
            spec = self.fx.write_spec(MUTANT_LOWER, run_extra=f'python = "{name}"')
            self.assertEqual(mutt_check.load_spec(spec).python, name)

    def test_missing_interpreter_is_rejected_at_load_with_exit_two(self):
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra='python = "/nonexistent/python"')
        with self.assertRaisesRegex(mutt_check.SpecError, "does not exist"):
            mutt_check.load_spec(spec)
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra='python = "no-such-python-xyz"')
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertIn("not found on PATH", err)
        self.assertEqual(out, "")

    def test_timeout_must_be_positive(self):
        self.assert_rejected("positive", MUTANT_LOWER, run_extra="timeout = 0")
        self.assert_rejected("positive", MUTANT_LOWER, run_extra="timeout = true")


class CliTest(MutcheckCase):
    def test_only_selects_named_mutants(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_COLLAPSE)
        code, out, _ = run_main(str(spec), "--only", "lowercase_dropped")
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)
        self.assertNotIn("collapse_dropped", out)
        self.assertIn("1 of 1 mutants applied", out)

    def test_only_with_an_unknown_name_exits_two(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, _, err = run_main(str(spec), "--only", "nope")
        self.assertEqual(code, mutt_check.EXIT_UNUSABLE)
        self.assertIn("no such mutant: nope", err)

    def test_list_prints_mutants_and_runs_nothing(self):
        spec = self.fx.write_spec(
            toml_mutant(*MUTANT_LOWER, why='"lowercasing is the contract"'),
            MUTANT_COLLAPSE)
        code, out, _ = run_main(str(spec), "--list")
        self.assertEqual(code, mutt_check.EXIT_PINNED)
        self.assertIn("lowercase_dropped  (1 edit: slugify.py)", out)
        self.assertIn("lowercasing is the contract", out)
        self.assertNotIn("caught", out)
        self.assertNotIn("SURVIVED", out)

    def test_json_report_carries_every_verdict_and_the_exit_code(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_COLLAPSE,
                                  ("gone", "slugify.py", "absent", "x"))
        code, out, _ = run_main(str(spec), "--json")
        report = json.loads(out)
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertEqual(report["control"]["outcome"], "green")
        self.assertEqual([m["outcome"] for m in report["mutants"]],
                         ["caught", "survived", "stale"])
        self.assertEqual(report["survived"], ["collapse_dropped"])
        self.assertEqual(report["stale"], ["gone"])
        self.assertEqual(report["broken"], [])
        self.assertEqual((report["selected"], report["applied"]), (3, 2))
        self.assertEqual(report["exit_code"], mutt_check.EXIT_UNPINNED)

    def test_keep_reports_a_sandbox_that_still_exists(self):
        import shutil
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, _ = run_main(str(spec), "--keep", "--json")
        report = json.loads(out)
        paths = [report["control"]["sandbox"]] + [
            m["sandbox"] for m in report["mutants"]]
        try:
            for p in paths:
                self.assertTrue(p and Path(p).is_dir(), p)
            kept = Path(report["mutants"][0]["sandbox"]) / "project" / "slugify.py"
            self.assertNotIn("text.lower()", kept.read_text())
        finally:
            for p in paths:
                shutil.rmtree(p, ignore_errors=True)

    def test_root_override_runs_a_spec_kept_outside_the_tree(self):
        outside = Path(self._tmp.name) / "specs" / "slugify.toml"
        outside.parent.mkdir()
        outside.write_text(self.fx.write_spec(MUTANT_LOWER).read_text())
        self.fx.spec_path.unlink()
        code, out, _ = run_main(str(outside), "--root", str(self.fx.root))
        self.assertEqual(code, mutt_check.EXIT_PINNED, out)

    def test_summary_counts_come_from_the_run_not_the_spec(self):
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_STRIP,
                                  ("gone", "slugify.py", "absent", "x"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutt_check.EXIT_UNPINNED)
        self.assertIn("2 of 3 mutants applied, control green, 0 survived, "
                      "1 stale, 0 broken: gone", out)


if __name__ == "__main__":
    unittest.main()
