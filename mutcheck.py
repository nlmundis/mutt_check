#!/usr/bin/env python3
"""mutcheck: prove a test suite catches the defects it claims to pin.

A green suite is evidence that nothing the suite checks is broken. It is
not evidence that any particular design decision is pinned down, because a
decision no test reaches can be reverted with every test still green.
mutcheck takes a curated list of mutants, each one reverting one
load-bearing decision in the code under test, applies them one at a time
to a throwaway copy of the project, and requires the suite to go red for
every one.

Three rules make the verdicts mean something:

* The unmutated control runs first and must be green. A suite that is
  already red reports every mutant as caught, including a no-op.
* An anchor must appear exactly once. A first-occurrence match that
  happens to hit the right line is one refactor away from retargeting
  silently.
* A mutant whose anchor is gone is STALE, and STALE fails the run. A
  check that rotted under a refactor tested nothing, and reporting that
  as success is the failure this tool exists to prevent.

Nothing in the project is modified. The spec format is in README.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Iterable, Sequence

__version__ = "0.1.0"

DEFAULT_SPEC = "mutcheck.toml"

#: Never copied into the sandbox. A spec's ``ignore`` list extends this.
DEFAULT_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", "__pycache__", "*.pyc", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", "venv", ".venv",
)

EXIT_PINNED = 0      # control green, every mutant caught
EXIT_UNPINNED = 1    # at least one mutant survived or went stale
EXIT_UNUSABLE = 2    # control red, spec invalid, or nothing could run

CONTROL_NAME = "control"


class SpecError(ValueError):
    """The spec cannot be run as written; the message says what to fix."""


@dataclasses.dataclass(frozen=True)
class Edit:
    """One exact-text replacement. ``file`` is empty in stage mode."""

    file: str
    find: str
    replace: str


@dataclasses.dataclass(frozen=True)
class Mutant:
    """One reverted decision: a name, its edits, and optionally its own suites."""

    name: str
    edits: tuple[Edit, ...]
    suites: tuple[str, ...] | None = None
    why: str = ""


@dataclasses.dataclass(frozen=True)
class Stage:
    """An external file the suite reads through an environment variable.

    ``as_path`` set: the mutated copy lands at ``<tmp>/<as_path>`` and
    ``env`` names ``<tmp>``. Unset: ``env`` names the mutated copy itself.
    """

    file: Path
    env: str
    as_path: str | None


@dataclasses.dataclass(frozen=True)
class Spec:
    """Everything one run needs, parsed and validated."""

    root: Path
    python: str
    suites: tuple[str, ...]
    command: tuple[str, ...] | None
    ignore: tuple[str, ...]
    allow_skips: bool
    stage: Stage | None
    mutants: tuple[Mutant, ...]


@dataclasses.dataclass(frozen=True)
class Verdict:
    """What one sandbox run established.

    ``outcome`` is ``green`` or ``red`` for the control, and ``caught``,
    ``survived`` or ``stale`` for a mutant.
    """

    name: str
    outcome: str
    detail: str = ""
    sandbox: str | None = None


# --- spec ----------------------------------------------------------------


def load_spec(path: Path, root: Path | None = None) -> Spec:
    """Parse and validate the TOML spec at ``path``.

    The project root defaults to the spec's directory. ``root`` overrides
    it, which keeps a spec outside the tree it exercises.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"no spec at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    return parse_spec(raw, (root or path.resolve().parent).resolve())


def parse_spec(raw: dict, root: Path) -> Spec:
    """Validate a decoded spec against ``root``; every problem is a SpecError."""
    if not root.is_dir():
        raise SpecError(f"project root is not a directory: {root}")
    run = _table(raw, "run")
    suites = _strings(run, "suites", "[run]")
    command = _strings(run, "command", "[run]")
    if not suites and not command:
        raise SpecError("[run] needs `suites` (unittest modules) or `command`")
    if suites and command:
        raise SpecError("[run] takes `suites` or `command`, not both")

    python = run.get("python", sys.executable)
    if not isinstance(python, str) or not python:
        raise SpecError("[run] python must be a non-empty string")
    if os.sep in python and not os.path.isabs(python):
        python = str(root / python)

    allow_skips = run.get("allow_skips", False)
    if not isinstance(allow_skips, bool):
        raise SpecError("[run] allow_skips must be true or false")

    stage = _parse_stage(raw.get("stage"), root)
    entries = raw.get("mutant", [])
    if not isinstance(entries, list) or not entries:
        raise SpecError("no [[mutant]] entries")
    mutants = tuple(_parse_mutant(m, i, stage) for i, m in enumerate(entries))

    seen: set[str] = set()
    for m in mutants:
        if m.name in seen:
            raise SpecError(f"duplicate mutant name: {m.name}")
        seen.add(m.name)
        if m.suites and command:
            raise SpecError(
                f"{m.name}: per-mutant `suites` needs the default unittest "
                "runner; drop [run] command or the override")
    return Spec(
        root=root, python=python, suites=suites, command=command or None,
        ignore=DEFAULT_IGNORE + _strings(run, "ignore", "[run]"),
        allow_skips=allow_skips, stage=stage, mutants=mutants,
    )


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise SpecError(f"[{key}] must be a table")
    return value


def _strings(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v for v in value):
        raise SpecError(f"{where} {key} must be a list of non-empty strings")
    return tuple(value)


def _parse_stage(raw: object, root: Path) -> Stage | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("[stage] must be a table")
    file = raw.get("file")
    env = raw.get("env")
    as_path = raw.get("as")
    if not isinstance(file, str) or not file:
        raise SpecError("[stage] file must name the file the suite reads")
    if not isinstance(env, str) or not env:
        raise SpecError("[stage] env must name the variable the suite reads")
    if as_path is not None and (not isinstance(as_path, str) or not as_path
                                or os.path.isabs(as_path)
                                or ".." in Path(as_path).parts):
        raise SpecError("[stage] `as` must be a relative path")
    path = Path(file).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise SpecError(f"[stage] file does not exist: {path}")
    return Stage(file=path.resolve(), env=env, as_path=as_path)


def _parse_mutant(raw: object, index: int, stage: Stage | None) -> Mutant:
    where = f"[[mutant]] #{index + 1}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be a table")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SpecError(f"{where} needs a non-empty name")
    if name == CONTROL_NAME:
        raise SpecError(f"{where}: `{CONTROL_NAME}` is reserved for the control run")
    where = f"mutant {name!r}"

    inline = {k: raw[k] for k in ("file", "find", "replace") if k in raw}
    listed = raw.get("edit")
    if inline and listed is not None:
        raise SpecError(f"{where}: use file/find/replace or [[mutant.edit]], not both")
    if listed is None:
        listed = [inline]
    if not isinstance(listed, list) or not listed:
        raise SpecError(f"{where}: needs at least one edit")
    edits = tuple(_parse_edit(e, i, where, stage) for i, e in enumerate(listed))

    suites = raw.get("suites")
    if suites is not None:
        suites = _strings({"suites": suites}, "suites", where)
        if not suites:
            raise SpecError(f"{where}: suites override must not be empty")
    why = raw.get("why", "")
    if not isinstance(why, str):
        raise SpecError(f"{where}: why must be a string")
    return Mutant(name=name, edits=edits, suites=suites, why=why)


def _parse_edit(raw: object, index: int, where: str, stage: Stage | None) -> Edit:
    where = f"{where} edit #{index + 1}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be a table")
    find = raw.get("find")
    replace = raw.get("replace")
    if not isinstance(find, str) or not find:
        raise SpecError(f"{where}: find must be a non-empty string")
    if not isinstance(replace, str):
        raise SpecError(f"{where}: replace must be a string (empty deletes)")
    if find == replace:
        raise SpecError(f"{where}: find and replace are identical, a no-op mutant")
    file = raw.get("file")
    if stage is not None:
        if file is not None:
            raise SpecError(f"{where}: in stage mode every edit targets "
                            f"[stage] file; drop `file`")
        return Edit(file="", find=find, replace=replace)
    if not isinstance(file, str) or not file:
        raise SpecError(f"{where}: file must name a path under the project root")
    if os.path.isabs(file) or ".." in Path(file).parts:
        raise SpecError(f"{where}: file must be relative to the root, no `..`")
    return Edit(file=file, find=find, replace=replace)


# --- sandbox ---------------------------------------------------------------


def apply_edits(edits: Iterable[Edit],
                read: Callable[[str], str | None],
                write: Callable[[str, str], None]) -> str | None:
    """Apply every edit exactly once, or return why the mutant is stale.

    All anchors are checked before anything is written, so a mutant whose
    second edit is stale leaves no half-applied first edit behind.
    """
    texts: dict[str, str] = {}
    for edit in edits:
        if edit.file not in texts:
            text = read(edit.file)
            if text is None:
                return f"cannot read {edit.file or 'staged file'}"
            texts[edit.file] = text
        count = texts[edit.file].count(edit.find)
        if count != 1:
            label = edit.file or "staged file"
            return f"anchor appears {count}x in {label}: {_excerpt(edit.find)}"
        texts[edit.file] = texts[edit.file].replace(edit.find, edit.replace, 1)
    for file, text in texts.items():
        write(file, text)
    return None


def _excerpt(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[:limit - 3] + "...")


def _read_under(root: Path) -> Callable[[str], str | None]:
    def read(file: str) -> str | None:
        try:
            return (root / file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return read


def build_command(spec: Spec, suites: Sequence[str] | None) -> tuple[str, ...]:
    """The suite command for one run: the spec's, or unittest over ``suites``."""
    if spec.command:
        return spec.command
    # -B: never write bytecode. CPython validates a cached .pyc against the
    # source's size and whole-second mtime, so two mutants of equal length
    # written within a second can run the previous mutant's bytecode.
    return (spec.python, "-B", "-m", "unittest", *(suites or spec.suites))


def run_sandboxed(spec: Spec, edits: Sequence[Edit],
                  suites: Sequence[str] | None = None,
                  keep: bool = False,
                  ) -> tuple[str | None, subprocess.CompletedProcess | None, Path]:
    """Copy or stage, apply ``edits``, run the suite once.

    Returns ``(stale_reason, completed, sandbox)``. Exactly one of the
    first two is None. The sandbox is deleted unless ``keep`` is set.
    """
    tmp = Path(tempfile.mkdtemp(prefix="mutcheck-"))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        if spec.stage is None:
            work = tmp / "project"
            shutil.copytree(spec.root, work, symlinks=True,
                            ignore=shutil.ignore_patterns(*spec.ignore))
            stale = apply_edits(
                edits, _read_under(work),
                lambda f, t: (work / f).write_text(t, encoding="utf-8"))
            cwd = work
        else:
            stage = spec.stage
            staged = tmp / (stage.as_path or stage.file.name)
            staged.parent.mkdir(parents=True, exist_ok=True)
            try:
                source: str | None = stage.file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                source = None
            staged.write_text(source or "", encoding="utf-8")
            stale = apply_edits(
                edits, lambda _f: source,
                lambda _f, t: staged.write_text(t, encoding="utf-8"))
            if source is None and not edits:
                stale = f"cannot read {stage.file}"
            cwd = spec.root
            env[stage.env] = str(tmp if stage.as_path else staged)
        if stale is not None:
            return stale, None, tmp
        completed = subprocess.run(
            build_command(spec, suites), cwd=cwd, env=env,
            capture_output=True, text=True, check=False)
        return None, completed, tmp
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


# --- verdicts --------------------------------------------------------------

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
_SKIP_RE = re.compile(r"skipped=(\d+)|(\d+) skipped")
_FAILED_RE = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)


def _output(completed: subprocess.CompletedProcess) -> str:
    return (completed.stderr or "") + "\n" + (completed.stdout or "")


def _tail(completed: subprocess.CompletedProcess, limit: int = 120) -> str:
    lines = [ln.strip() for ln in _output(completed).splitlines() if ln.strip()]
    last = lines[-1] if lines else f"exit {completed.returncode}, no output"
    return last if len(last) <= limit else last[:limit - 3] + "..."


def control_verdict(spec: Spec, keep: bool = False) -> Verdict:
    """Run the unmutated tree. Red, or skipped tests, means no verdict counts."""
    stale, completed, sandbox = run_sandboxed(spec, (), keep=keep)
    where = str(sandbox) if keep else None
    if completed is None:
        return Verdict(CONTROL_NAME, "red", stale or "could not run", where)
    text = _output(completed)
    skipped = sum(int(a or b) for a, b in _SKIP_RE.findall(text))
    if completed.returncode != 0:
        return Verdict(CONTROL_NAME, "red", _tail(completed), where)
    if skipped and not spec.allow_skips:
        return Verdict(CONTROL_NAME, "red",
                       f"{skipped} test(s) skipped; a skipped test can catch "
                       "nothing. Fix them or set allow_skips = true", where)
    ran = _RAN_RE.search(text)
    detail = f"{ran.group(1)} tests" if ran else _tail(completed)
    if skipped:
        detail += f", {skipped} skipped"
    return Verdict(CONTROL_NAME, "green", detail, where)


def mutant_verdict(spec: Spec, mutant: Mutant, keep: bool = False) -> Verdict:
    """Apply one mutant and judge it: caught, survived, or stale."""
    stale, completed, sandbox = run_sandboxed(
        spec, mutant.edits, mutant.suites, keep=keep)
    where = str(sandbox) if keep else None
    if completed is None:
        return Verdict(mutant.name, "stale", stale or "", where)
    if completed.returncode == 0:
        return Verdict(mutant.name, "survived", "", where)
    failed = _FAILED_RE.findall(_output(completed))
    detail = ", ".join(dict.fromkeys(failed)) if failed else _tail(completed)
    return Verdict(mutant.name, "caught", detail, where)


@dataclasses.dataclass
class Report:
    """The whole run: the control, each mutant in order, and the exit code."""

    control: Verdict
    mutants: list[Verdict]
    declared: int

    @property
    def applied(self) -> int:
        return sum(1 for v in self.mutants if v.outcome != "stale")

    @property
    def survived(self) -> list[str]:
        return [v.name for v in self.mutants if v.outcome == "survived"]

    @property
    def stale(self) -> list[str]:
        return [v.name for v in self.mutants if v.outcome == "stale"]

    @property
    def exit_code(self) -> int:
        if self.control.outcome != "green":
            return EXIT_UNUSABLE
        if self.survived or self.stale:
            return EXIT_UNPINNED
        return EXIT_PINNED

    def summary(self) -> str:
        """One line a reader can quote: counts from this run, not the spec."""
        if self.control.outcome != "green":
            return ("control RED: no mutant verdict would mean anything. "
                    f"{self.control.detail}")
        line = (f"{self.applied} of {self.declared} mutants applied, control "
                f"green, {len(self.survived)} survived, {len(self.stale)} stale")
        names = self.survived + self.stale
        return line + (f": {', '.join(names)}" if names else "")

    def as_json(self) -> dict:
        return {
            "control": dataclasses.asdict(self.control),
            "mutants": [dataclasses.asdict(v) for v in self.mutants],
            "declared": self.declared,
            "applied": self.applied,
            "survived": self.survived,
            "stale": self.stale,
            "exit_code": self.exit_code,
        }


def check(spec: Spec, only: Sequence[str] = (), keep: bool = False,
          emit: Callable[[Verdict], None] | None = None) -> Report:
    """Run the control and then every selected mutant.

    ``emit`` receives each verdict as it lands, so a long run shows
    progress. A red control stops the run before any mutant is applied.
    """
    selected = [m for m in spec.mutants if not only or m.name in only]
    unknown = set(only) - {m.name for m in spec.mutants}
    if unknown:
        raise SpecError(f"no such mutant: {', '.join(sorted(unknown))}")
    notify = emit or (lambda _v: None)
    control = control_verdict(spec, keep=keep)
    notify(control)
    report = Report(control=control, mutants=[], declared=len(selected))
    if control.outcome != "green":
        return report
    for mutant in selected:
        verdict = mutant_verdict(spec, mutant, keep=keep)
        notify(verdict)
        report.mutants.append(verdict)
    return report


# --- CLI -------------------------------------------------------------------


def format_verdict(v: Verdict, width: int) -> str:
    label = {"green": "green", "red": "RED", "caught": "caught",
             "survived": "SURVIVED", "stale": "STALE"}[v.outcome]
    line = f"  {v.name:<{width}}  {label:<8}"
    if v.detail:
        line += f"  {v.detail}"
    if v.sandbox:
        line += f"  [{v.sandbox}]"
    return line.rstrip()


def list_mutants(spec: Spec) -> str:
    """The spec's mutants as a reader would want them: name, files, why."""
    lines = []
    for m in spec.mutants:
        files = sorted({e.file for e in m.edits if e.file}) or (
            [str(spec.stage.file)] if spec.stage else [])
        head = f"{m.name}  ({len(m.edits)} edit{'s' if len(m.edits) != 1 else ''}"
        head += f": {', '.join(files)})" if files else ")"
        lines.append(head)
        if m.suites:
            lines.append(f"    suites: {' '.join(m.suites)}")
        if m.why:
            lines.append(f"    {m.why}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutcheck",
        description="Prove a test suite catches the defects it claims to pin.")
    parser.add_argument("spec", nargs="?", default=DEFAULT_SPEC,
                        help=f"TOML spec (default: {DEFAULT_SPEC})")
    parser.add_argument("--root", type=Path,
                        help="project root (default: the spec's directory)")
    parser.add_argument("--only", action="append", default=[], metavar="NAME",
                        help="run only this mutant; repeatable")
    parser.add_argument("--list", action="store_true",
                        help="list the mutants and run nothing")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of text")
    parser.add_argument("--keep", action="store_true",
                        help="keep each sandbox on disk and print its path")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = load_spec(Path(args.spec), args.root)
        if args.list:
            print(list_mutants(spec))
            return EXIT_PINNED
        width = max([len(CONTROL_NAME)] + [len(m.name) for m in spec.mutants])
        emit = None if args.json else (
            lambda v: print(format_verdict(v, width), flush=True))
        report = check(spec, only=args.only, keep=args.keep, emit=emit)
    except SpecError as exc:
        print(f"mutcheck: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.json:
        print(json.dumps(report.as_json(), indent=2))
    else:
        print()
        print(report.summary())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
