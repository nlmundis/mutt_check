"""Tests for the sandbox and the run: isolation, processes, edits, BROKEN.

Each class pins a family of decisions the dogfood spec reverts by name, so
`mutcheck.toml` can point a mutant at the class that is supposed to catch it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import mock

import mutcheck
from tests.test_mutcheck import MUTANT_LOWER, SLUGIFY, MutcheckCase, run_main, toml_mutant

MUTANT_STRIP = ("strip_dropped", "slugify.py", 'return text.strip("-")', "return text")
MUTANT_IMPORT = ("missing", "slugify.py", "import re", "import re_mutcheck_missing")
#: Compiles and is not an ImportError, so unittest given a module by name lets
#: it escape: only the raised-straight-through rule can call it BROKEN.
MUTANT_RAISES = ("raises", "slugify.py", "import re",
                 'import re\nraise RuntimeError("mutant raised at import")')


def add_suite(root: Path, module: str, body: str) -> None:
    (root / "tests" / f"{module}.py").write_text(textwrap.dedent(body))


def use_suites(spec: Path, *suites: str) -> None:
    listed = ", ".join(f'"{s}"' for s in suites)
    spec.write_text(spec.read_text().replace(
        'suites = ["tests.test_slugify"]', f"suites = [{listed}]", 1))


class IsolationTest(MutcheckCase):
    """Nothing the run does may reach the real project or outlive the run."""

    def test_relative_link_out_of_the_project_is_refused(self):
        (Path(self._tmp.name) / "shared").mkdir()
        os.symlink("../shared", self.fx.root / "shared")
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("shared -> ../shared", err)

    def test_link_that_stays_inside_the_project_is_allowed(self):
        os.symlink("slugify.py", self.fx.root / "alias.py")
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)

    def test_read_under_refuses_a_link_out_of_the_copy(self):
        work = Path(self._tmp.name) / "work"
        work.mkdir()
        outside = Path(self._tmp.name) / "outside.py"
        outside.write_text("X = 1\n")
        os.symlink(outside, work / "x.py")
        with self.assertRaisesRegex(mutcheck._Unreadable, "outside the sandbox"):
            mutcheck._read_under(work)("x.py")

    def test_temp_dir_inside_the_project_is_refused(self):
        inside = self.fx.root / "tmpdir"
        inside.mkdir()
        spec = self.fx.write_spec(MUTANT_LOWER)
        with mock.patch.object(tempfile, "tempdir", str(inside)):
            code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("is inside the project", err)
        self.assertEqual(list(inside.iterdir()), [])

    def test_copy_is_first_on_pythonpath(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        with mock.patch.dict(os.environ, {"PYTHONSAFEPATH": "1",
                                          "PYTHONPATH": str(self.fx.root)}):
            code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertRegex(out, r"lowercase_dropped\s+caught")

    def test_read_only_target_is_still_mutated_in_the_copy(self):
        target = self.fx.root / "slugify.py"
        target.chmod(0o444)
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)

    def test_read_only_directory_does_not_leak_a_sandbox(self):
        frozen = self.fx.root / "frozen"
        frozen.mkdir()
        (frozen / "data.txt").write_text("x")
        frozen.chmod(0o555)
        controlled = Path(self._tmp.name) / "tmproot"
        controlled.mkdir()
        spec = self.fx.write_spec(MUTANT_LOWER)
        try:
            with mock.patch.object(tempfile, "tempdir", str(controlled)):
                code, out, err = run_main(str(spec))
        finally:
            frozen.chmod(0o755)
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertNotIn("could not remove", err)
        self.assertEqual(sorted(controlled.glob("mutcheck-*")), [])

    def test_default_run_removes_every_sandbox(self):
        controlled = Path(self._tmp.name) / "tmproot"
        controlled.mkdir()
        spec = self.fx.write_spec(MUTANT_LOWER, MUTANT_STRIP)
        with mock.patch.object(tempfile, "tempdir", str(controlled)):
            code, out, _ = run_main(str(spec), "--json")
        report = json.loads(out)
        self.assertEqual(code, mutcheck.EXIT_PINNED)
        self.assertIsNone(report["control"]["sandbox"])
        self.assertEqual([m["sandbox"] for m in report["mutants"]], [None, None])
        self.assertEqual(report["leaked_sandboxes"], 0)
        self.assertEqual(sorted(controlled.glob("mutcheck-*")), [])

    def test_no_bytecode_is_written_into_a_kept_sandbox(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        self.assertIn("-B", mutcheck.build_command(mutcheck.load_spec(spec), None))
        code, out, _ = run_main(str(spec), "--keep", "--json")
        report = json.loads(out)
        kept = [report["control"]["sandbox"]] + [m["sandbox"] for m in report["mutants"]]
        try:
            self.assertEqual(code, mutcheck.EXIT_PINNED)
            for path in kept:
                self.assertEqual(list(Path(path).rglob("__pycache__")), [], path)
        finally:
            for path in kept:
                shutil.rmtree(path, ignore_errors=True)

    def test_interpreter_that_cannot_start_exits_two(self):
        python = self.fx.root / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra=f'python = "{python}"')
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE)
        self.assertIn("cannot run", err)
        self.assertEqual(out, "")
        code, out, err = run_main(str(spec), "--keep")
        kept = re.search(r"\[sandbox kept at (.+?)\]", err)
        try:
            self.assertEqual(code, mutcheck.EXIT_UNUSABLE)
            self.assertIsNotNone(kept, err)
        finally:
            if kept:
                shutil.rmtree(kept.group(1), ignore_errors=True)

    def test_file_that_cannot_be_copied_is_named_with_the_way_out(self):
        os.mkfifo(self.fx.root / "events.fifo")
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("events.fifo", err)
        self.assertIn("[run] ignore", err)
        self.assertNotIn(str(self.fx.root.resolve()) + "/events.fifo", err)

    def test_recursion_while_copying_exits_two(self):
        spec = self.fx.write_spec(MUTANT_LOWER)
        with mock.patch.object(mutcheck.shutil, "copytree",
                               side_effect=RecursionError("maximum recursion depth")):
            code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("RecursionError", err)


class ProcessTest(MutcheckCase):
    def test_helper_left_running_neither_holds_the_run_nor_survives_it(self):
        pids = Path(self._tmp.name) / "pids"
        add_suite(self.fx.root, "test_helper", '''\
            import os, subprocess, sys, unittest
            from slugify import slugify

            class HelperTest(unittest.TestCase):
                def test_lowercases(self):
                    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    with open(os.environ["HELPER_PID_FILE"], "a") as f:
                        f.write(f"{helper.pid}\\n")
                    self.assertEqual(slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra="timeout = 30")
        use_suites(spec, "tests.test_helper")
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"HELPER_PID_FILE": str(pids)}):
            code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertLess(time.monotonic() - started, 20)
        alive = []
        for pid in (int(line) for line in pids.read_text().split()):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                alive.append(pid)
                os.kill(pid, 9)
        self.assertEqual(alive, [])


class EditSemanticsTest(MutcheckCase):
    def test_overlapping_anchor_is_ambiguous(self):
        (self.fx.root / "sep.py").write_text('X = "---"\n')
        spec = self.fx.write_spec(("dashes", "sep.py", "--", "-"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED)
        self.assertRegex(out, r"dashes\s+STALE\s+anchor appears 2x in sep.py")

    def test_multi_line_anchor_matches_a_crlf_file_in_its_own_endings(self):
        (self.fx.root / "slugify.py").write_bytes(SLUGIFY.replace("\n", "\r\n").encode())
        spec = self.fx.write_spec(("two_lines", "slugify.py",
                                   "text = text.lower()\n    text = re.sub",
                                   "text = text\n    text = re.sub"))
        loaded = mutcheck.load_spec(spec)
        result = mutcheck.run_once(loaded, loaded.mutants[0].edits, keep=True)
        try:
            self.assertIsNone(result.problem, result.detail)
            copied = (result.sandbox / "project" / "slugify.py").read_bytes()
            self.assertNotIn(b"text.lower()", copied)
            self.assertEqual(copied.count(b"\n"), copied.count(b"\r\n"))
        finally:
            shutil.rmtree(result.sandbox, ignore_errors=True)

    def test_two_spellings_of_one_file_compose(self):
        spec = self.fx.write_spec(textwrap.dedent('''\
            [[mutant]]
            name = "both"
            [[mutant.edit]]
            file = "slugify.py"
            find = "text = text.lower()"
            replace = "text = text"
            [[mutant.edit]]
            file = "./slugify.py"
            find = 'return text.strip("-")'
            replace = "return text"
            '''))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out)
        self.assertRegex(out, r"both\s+caught\s+.*test_lowercases.*test_strips_edges")

    def test_case_variant_spelling_is_one_file_where_the_filesystem_says_so(self):
        spec = self.fx.write_spec(textwrap.dedent('''\
            [[mutant]]
            name = "both"
            [[mutant.edit]]
            file = "slugify.py"
            find = "text = text.lower()"
            replace = "text = text"
            [[mutant.edit]]
            file = "SLUGIFY.py"
            find = 'return text.strip("-")'
            replace = "return text"
            '''))
        case_insensitive = (self.fx.root / "SLUGIFY.py").exists()
        code, out, _ = run_main(str(spec))
        if case_insensitive:
            self.assertEqual(code, mutcheck.EXIT_PINNED, out)
            self.assertRegex(out, r"both\s+caught\s+.*test_lowercases.*test_strips_edges")
        else:
            self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
            self.assertRegex(out, r"both\s+STALE\s+cannot read SLUGIFY.py")


class BrokenTest(MutcheckCase):
    def test_mutant_that_does_not_compile_is_broken_when_imported_inside_a_test(self):
        add_suite(self.fx.root, "test_inbody", '''\
            import unittest

            class InBodyTest(unittest.TestCase):
                def test_lowercases(self):
                    from slugify import slugify
                    self.assertEqual(slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(("typo", "slugify.py", "text = text.lower()", "text = ("))
        use_suites(spec, "tests.test_inbody")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"typo\s+BROKEN\s+mutant does not compile: .*\(slugify.py, line \d+\)")

    def test_mutant_that_does_not_compile_is_broken_in_stage_mode(self):
        hook = Path(self._tmp.name) / "deployed" / "hook.py"
        hook.parent.mkdir()
        hook.write_text(SLUGIFY)
        add_suite(self.fx.root, "test_hook", '''\
            import importlib.util, os, unittest

            class HookTest(unittest.TestCase):
                def test_lowercases(self):
                    spec = importlib.util.spec_from_file_location("hook", os.environ["HOOK_PATH"])
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    self.assertEqual(mod.slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(
            toml_mutant("typo", None, "text = text.lower()", "text = ("),
            top=f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\n')
        use_suites(spec, "tests.test_hook")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"typo\s+BROKEN\s+mutant does not compile: .*\(hook.py, line \d+\)")

    def test_compile_check_skips_a_file_this_interpreter_cannot_compile(self):
        (self.fx.root / "legacy.py").write_text('print "hello"\nX = 1\n')
        spec = self.fx.write_spec(("legacy", "legacy.py", "X = 1", "X = 2"))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"legacy\s+SURVIVED")

    def test_shell_wrapped_unittest_keeps_broken_detection(self):
        command = f"{sys.executable} -B -m unittest tests.test_slugify"
        spec = self.fx.write_spec(MUTANT_RAISES, run_extra=f'command = ["sh", "-c", "{command}"]')
        spec.write_text(spec.read_text().replace('suites = ["tests.test_slugify"]\n', "", 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"raises\s+BROKEN\s+RuntimeError: mutant raised at import")

    def test_summary_text_a_test_prints_does_not_decide_the_run(self):
        add_suite(self.fx.root, "test_printer", '''\
            import unittest
            from slugify import slugify

            class PrinterTest(unittest.TestCase):
                def test_lowercases(self):
                    print("Ran 0 tests in 0.000s")
                    print("OK (skipped=3)")
                    self.assertEqual(slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(MUTANT_LOWER)
        use_suites(spec, "tests.test_printer")
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertRegex(out, r"control\s+green\s+1 tests\n")

    def test_broken_mutants_count_as_applied(self):
        spec = self.fx.write_spec(MUTANT_IMPORT)
        code, out, _ = run_main(str(spec), "--json")
        report = json.loads(out)
        self.assertEqual(code, mutcheck.EXIT_UNPINNED)
        self.assertEqual((report["selected"], report["applied"]), (1, 1))
        self.assertEqual(report["broken"], ["missing"])


class ControlCoverageTest(MutcheckCase):
    def test_control_runs_a_suite_only_a_mutant_names(self):
        # Otherwise an override naming an already-red suite reports caught.
        add_suite(self.fx.root, "test_red", '''\
            import unittest

            class RedTest(unittest.TestCase):
                def test_fails(self):
                    self.assertTrue(False)
            ''')
        spec = self.fx.write_spec(toml_mutant(
            "bogus", "slugify.py", 'return text.strip("-")', "return text",
            suites='["tests.test_red"]'))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertRegex(out, r"(?m)^  control\s+RED")
        self.assertNotIn("caught", out)


class CompileGuardTest(MutcheckCase):
    IN_BODY = '''\
        import unittest

        class InBodyTest(unittest.TestCase):
            def test_lower(self):
                from slugify import slugify
                self.assertEqual(slugify("Hello"), "hello")
        '''

    def test_a_byte_order_mark_does_not_disable_the_compile_check(self):
        (self.fx.root / "slugify.py").write_bytes(b"\xef\xbb\xbf" + SLUGIFY.encode())
        add_suite(self.fx.root, "test_inbody", self.IN_BODY)
        spec = self.fx.write_spec(("typo", "slugify.py", "text = text.lower()", "text = ("))
        use_suites(spec, "tests.test_inbody")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"typo\s+BROKEN\s+mutant does not compile")
        self.assertEqual((self.fx.root / "slugify.py").read_bytes()[:3], b"\xef\xbb\xbf")

    def test_a_target_with_no_suffix_is_compile_checked(self):
        hook = Path(self._tmp.name) / "deployed_hook"
        hook.write_text(SLUGIFY)
        add_suite(self.fx.root, "test_hook", '''\
            import importlib.machinery, importlib.util, os, unittest

            class HookTest(unittest.TestCase):
                def test_lower(self):
                    loader = importlib.machinery.SourceFileLoader("h", os.environ["HOOK_PATH"])
                    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("h", loader))
                    loader.exec_module(mod)
                    self.assertEqual(mod.slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(
            toml_mutant("typo", None, "text = text.lower()", "text = ("),
            top=f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\n')
        use_suites(spec, "tests.test_hook")
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"typo\s+BROKEN\s+mutant does not compile: .*\(deployed_hook")

    def test_a_data_file_is_not_judged_as_python(self):
        (self.fx.root / "fixture.json").write_text('{"a": 1}\n')
        spec = self.fx.write_spec(("json", "fixture.json", '{"a": 1}', '{"a": 1'))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"json\s+SURVIVED")

    def test_a_refusal_is_confirmed_with_the_runs_own_interpreter(self):
        accepts = self.fx.root / "accepts"
        accepts.write_text("#!/bin/sh\nexit 0\n")
        accepts.chmod(0o755)
        refuses = self.fx.root / "refuses"
        refuses.write_text("#!/bin/sh\nexit 1\n")
        refuses.chmod(0o755)
        self.assertIsNone(mutcheck._compile_error("m.py", "x = 1\n", "x = (\n", str(accepts)))
        self.assertIn("does not compile",
                      mutcheck._compile_error("m.py", "x = 1\n", "x = (\n", str(refuses)))


class LineEndingTest(MutcheckCase):
    def apply(self, find, replace, text):
        written = {}
        reason = mutcheck.apply_edits((mutcheck.Edit("f.py", find, replace),),
                                      lambda _f: text, written.__setitem__)
        return reason, written.get("f.py")

    def test_an_anchor_starting_with_a_newline_is_applied_in_crlf(self):
        reason, written = self.apply("\nb", "\nX\nb", "a\r\nb\r\nc\r\n")
        self.assertIsNone(reason)
        self.assertEqual(written, "a\r\nX\r\nb\r\nc\r\n")

    def test_mixed_line_endings_are_named_as_the_reason(self):
        reason, written = self.apply("a\nb\nc", "x", "a\nb\r\nc\r\n")
        self.assertIn("only with line endings normalised", reason)
        self.assertIn("mixes CRLF and LF", reason)
        self.assertIsNone(written)


class ExclusionMessageTest(MutcheckCase):
    def test_a_target_the_copy_left_out_says_so(self):
        (self.fx.root / "vendor").mkdir()
        (self.fx.root / "vendor" / "mod.py").write_text("X = 1\n")
        os.symlink("vendor/mod.py", self.fx.root / "alias.py")
        spec = self.fx.write_spec(("aliased", "alias.py", "X = 1", "X = 2"),
                                  run_extra='ignore = ["vendor"]')
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNPINNED, out)
        self.assertRegex(out, r"aliased\s+STALE\s+alias.py is in the project but not in "
                              r"the sandbox")


class SpecLinkTest(MutcheckCase):
    def test_a_spec_symlinked_into_the_project_is_not_a_link_out_of_it(self):
        elsewhere = Path(self._tmp.name) / "specs"
        elsewhere.mkdir()
        real = elsewhere / "spec.toml"
        real.write_text('[run]\nsuites = ["tests.test_slugify"]\n' + toml_mutant(*MUTANT_LOWER))
        link = self.fx.root / "linked.toml"
        os.symlink(real, link)
        code, out, err = run_main(str(link))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)

    def test_a_nested_link_out_of_the_project_is_refused(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.fx.root / "tests" / "fixtures").mkdir()
        os.symlink(outside, self.fx.root / "tests" / "fixtures" / "data")
        spec = self.fx.write_spec(MUTANT_LOWER)
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("tests/fixtures/data ->", err)


class LeakAccountingTest(MutcheckCase):
    def test_a_sandbox_that_cannot_be_removed_is_counted_in_the_summary(self):
        controlled = Path(self._tmp.name) / "tmproot"
        controlled.mkdir()
        spec = self.fx.write_spec(MUTANT_LOWER)
        with mock.patch.object(tempfile, "tempdir", str(controlled)):
            with mock.patch.object(mutcheck.shutil, "rmtree", side_effect=OSError("busy")):
                code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out)
        self.assertIn("2 sandbox(es) could not be removed", out)
        self.assertIn("could not remove sandbox", err)
        self.assertEqual(len(sorted(controlled.glob("mutcheck-*"))), 2)
        shutil.rmtree(controlled, ignore_errors=True)


class BytecodeTest(MutcheckCase):
    def test_no_bytecode_lands_in_the_sandbox_under_a_custom_command(self):
        # No -B here, so only PYTHONDONTWRITEBYTECODE in the run's env stops it.
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra=(
            f'command = ["{sys.executable}", "-m", "unittest", "tests.test_slugify"]'))
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        # A nested run inherits the flag from the run around it, which would
        # hide the very setting under test.
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            code, out, _ = run_main(str(spec), "--keep", "--json")
        report = json.loads(out)
        kept = [report["control"]["sandbox"]] + [m["sandbox"] for m in report["mutants"]]
        try:
            self.assertEqual(code, mutcheck.EXIT_PINNED, out)
            for path in kept:
                self.assertEqual(list(Path(path).rglob("__pycache__")), [], path)
        finally:
            for path in kept:
                shutil.rmtree(path, ignore_errors=True)


class TimeoutGroupTest(MutcheckCase):
    def test_a_timeout_kills_the_whole_group_of_a_wrapped_suite(self):
        pids = Path(self._tmp.name) / "pids"
        add_suite(self.fx.root, "test_hang", '''\
            import os, subprocess, sys, time, unittest

            class HangTest(unittest.TestCase):
                def test_hangs(self):
                    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    with open(os.environ["HELPER_PID_FILE"], "a") as handle:
                        handle.write(f"{helper.pid}\\n")
                    time.sleep(60)
            ''')
        wrapped = f"{sys.executable} -B -m unittest tests.test_hang; true"
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra=(
            f'timeout = 3\ncommand = ["sh", "-c", "{wrapped}"]'))
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        with mock.patch.dict(os.environ, {"HELPER_PID_FILE": str(pids)}):
            code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("timed out after 3s", out)
        alive = []
        for pid in (int(line) for line in pids.read_text().split()):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                alive.append(pid)
                os.kill(pid, 9)
        self.assertEqual(alive, [])


class PythonPathTest(MutcheckCase):
    def test_a_project_subdirectory_on_pythonpath_is_remapped_into_the_copy(self):
        src = self.fx.root / "src"
        src.mkdir()
        (src / "pkg.py").write_text(SLUGIFY)
        add_suite(self.fx.root, "test_pkg", '''\
            import unittest
            from pkg import slugify

            class PkgTest(unittest.TestCase):
                def test_lower(self):
                    self.assertEqual(slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(("lower", "src/pkg.py", "text = text.lower()", "text = text"))
        use_suites(spec, "tests.test_pkg")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(src)}):
            code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertRegex(out, r"lower\s+caught")


class StagingErrorTest(MutcheckCase):
    def test_a_stage_path_that_cannot_be_created_exits_two(self):
        hook = Path(self._tmp.name) / "hook.py"
        hook.write_text(SLUGIFY)
        spec = self.fx.write_spec(
            toml_mutant("lower", None, "text = text.lower()", "text = text"),
            top=f'[stage]\nfile = "{hook}"\nenv = "HOOK_PATH"\nas = "{"x" * 300}/hook.py"\n')
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_UNUSABLE, out)
        self.assertIn("cannot stage hook.py", err)


class RunnerDetectionTest(MutcheckCase):
    def test_a_command_with_unittest_in_its_path_is_not_treated_as_unittest(self):
        (self.fx.root / "check_unittest_compat.py").write_text(
            "import sys\nfrom slugify import slugify\n"
            "sys.exit(0 if slugify('Hello') == 'hello' else 1)\n")
        spec = self.fx.write_spec(MUTANT_LOWER, run_extra=(
            f'command = ["{sys.executable}", "check_unittest_compat.py"]'))
        spec.write_text(spec.read_text().replace(
            'suites = ["tests.test_slugify"]\n', "", 1))
        code, out, _ = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out)
        self.assertRegex(out, r"lowercase_dropped\s+caught")

    def test_load_failure_text_a_test_prints_is_not_a_load_failure(self):
        add_suite(self.fx.root, "test_printer", '''\
            import sys, unittest
            from slugify import slugify

            class PrinterTest(unittest.TestCase):
                def test_lower(self):
                    sys.stderr.write("ERROR: t (unittest.loader._FailedTest.t)\\n")
                    self.assertEqual(slugify("Hello"), "hello")
            ''')
        spec = self.fx.write_spec(MUTANT_LOWER)
        use_suites(spec, "tests.test_printer")
        code, out, err = run_main(str(spec))
        self.assertEqual(code, mutcheck.EXIT_PINNED, out + err)
        self.assertRegex(out, r"control\s+green")
        self.assertRegex(out, r"lowercase_dropped\s+caught")
