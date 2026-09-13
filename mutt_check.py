#!/usr/bin/env python3
"""mutt_check: prove a test suite catches the defects it claims to pin.

A green suite is evidence that nothing the suite checks is broken. It is
not evidence that any particular design decision is pinned down, because a
decision no test reaches can be reverted with every test still green.
mutt_check takes a curated list of mutants, each one reverting one
load-bearing decision in the code under test, applies them one at a time
to a throwaway copy of the project, and requires the suite to go red for
every one.

Four rules make the verdicts mean something:

* The unmutated control runs first and must be green. A suite that is
  already red reports every mutant as caught, including a no-op.
* An anchor must appear exactly once. A first-occurrence match that
  happens to hit the right line is one refactor away from retargeting
  silently.
* A mutant whose anchor is gone is STALE, and STALE fails the run. A
  check that rotted under a refactor tested nothing, and reporting that
  as success is the failure this tool exists to prevent.
* A mutant the suite could not even load is BROKEN, not caught. A red
  suite proves something only when tests ran and failed.

mutt_check never writes into the project: every edit lands in a temporary
copy, or in a staged copy of an external file, and a path that resolves
outside the sandbox through a symlink is refused. In copy mode the suite
runs inside the sandbox as well. In stage mode it runs in the real tree,
so any side effects of the suite itself are the suite's own.

The spec format is in README.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier
    import tomli as tomllib
from pathlib import Path
from typing import Callable, Iterable, Sequence

__version__ = "0.1.0"

DEFAULT_SPEC = "mutt_check.toml"

#: Never copied into the sandbox unless ``use_default_ignores = false``. A
#: spec's ``ignore`` list extends this.
DEFAULT_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", "__pycache__", "*.pyc", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", "venv", ".venv",
)

EXIT_PINNED = 0      # control green, every mutant caught
EXIT_UNPINNED = 1    # at least one mutant survived, went stale, or broke
EXIT_UNUSABLE = 2    # control red, spec invalid, or nothing could run

CONTROL_NAME = "control"

#: The keys each table accepts. A misspelled key that is silently dropped
#: changes the run without a word, so anything else is a spec error.
TOP_KEYS = frozenset({"run", "stage", "mutant"})
RUN_KEYS = frozenset({"suites", "command", "python", "ignore", "use_default_ignores",
                      "allow_skips", "timeout"})
STAGE_KEYS = frozenset({"file", "env", "as"})
MUTANT_KEYS = frozenset({"name", "why", "suites", "file", "find", "replace", "edit"})
EDIT_KEYS = frozenset({"file", "find", "replace"})


class SpecError(ValueError):
    """The spec cannot be run as written; the message says what to fix."""


class RunError(RuntimeError):
    """The run could not be carried out; the message says what broke.

    Raised for infrastructure faults (the sandbox could not be built, the
    interpreter could not be started), which are never a verdict.
    """


class _Unreadable(Exception):
    """A target file cannot be edited; ``str(exc)`` says why."""


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
    #: The file's text as read at load time. Every run stages this, so the
    #: control and each mutant judge the same content even if the real file
    #: changes while the run is going.
    source: str


@dataclasses.dataclass(frozen=True)
class Spec:
    """Everything one run needs, parsed and validated."""

    root: Path
    python: str
    suites: tuple[str, ...]
    command: tuple[str, ...] | None
    ignore: tuple[str, ...]
    allow_skips: bool
    timeout: float | None
    stage: Stage | None
    mutants: tuple[Mutant, ...]
    #: Where the spec was read from, so the copy can tell a spec symlinked
    #: into the project from a link that breaks the sandbox.
    spec_file: Path | None = None


@dataclasses.dataclass(frozen=True)
class Verdict:
    """What one sandbox run established.

    ``outcome`` is ``green`` or ``red`` for the control, and ``caught``,
    ``survived``, ``stale`` or ``broken`` for a mutant.
    """

    name: str
    outcome: str
    detail: str = ""
    sandbox: str | None = None
    #: The run's sandbox could not be removed afterwards.
    leaked: bool = False


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
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"cannot read spec {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    # The directory the spec was NAMED in, so a spec symlinked into a tree
    # runs against that tree rather than wherever the link points.
    return parse_spec(raw, (root or path.absolute().parent).resolve(),
                      path.absolute())


def parse_spec(raw: dict, root: Path, spec_file: Path | None = None) -> Spec:
    """Validate a decoded spec against ``root``; every problem is a SpecError."""
    if not root.is_dir():
        raise SpecError(f"project root is not a directory: {root}")
    _reject_unknown(raw, TOP_KEYS, "the spec")
    run = _table(raw, "run")
    _reject_unknown(run, RUN_KEYS, "[run]")
    suites = _strings(run, "suites", "[run]")
    command = _strings(run, "command", "[run]")
    if not suites and not command:
        raise SpecError("[run] needs `suites` (unittest modules) or `command`")
    if suites and command:
        raise SpecError("[run] takes `suites` or `command`, not both")
    if command and "python" in run:
        raise SpecError("[run] python has no effect with `command`, which names its "
                        "own interpreter; drop one of the two")

    python = _interpreter(run.get("python", sys.executable), root)
    allow_skips = run.get("allow_skips", False)
    if not isinstance(allow_skips, bool):
        raise SpecError("[run] allow_skips must be true or false")
    use_default_ignores = run.get("use_default_ignores", True)
    if not isinstance(use_default_ignores, bool):
        raise SpecError("[run] use_default_ignores must be true or false")
    timeout = run.get("timeout")
    if timeout is not None and (isinstance(timeout, bool)
                                or not isinstance(timeout, (int, float))
                                or timeout <= 0):
        raise SpecError("[run] timeout must be a positive number of seconds")

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
    extra_ignore = _strings(run, "ignore", "[run]")
    for pattern in extra_ignore:
        if "/" in pattern or os.sep in pattern:
            raise SpecError(f"[run] ignore pattern {pattern!r} contains a path "
                            "separator; patterns match one file or directory name")
    ignore = (DEFAULT_IGNORE if use_default_ignores else ()) + extra_ignore
    if stage is None:
        _reject_ignored_targets(mutants, ignore)
    return Spec(
        root=root, python=python, suites=suites, command=command or None,
        ignore=ignore, allow_skips=allow_skips,
        timeout=float(timeout) if timeout is not None else None,
        stage=stage, mutants=mutants, spec_file=spec_file,
    )


def _interpreter(python: object, root: Path) -> str:
    """Resolve ``[run] python`` and refuse one that could never start."""
    if not isinstance(python, str) or not python:
        raise SpecError("[run] python must be a non-empty string")
    if "/" in python or os.sep in python:
        if not os.path.isabs(python):
            python = str(root / python)
        if not os.path.isfile(python):
            raise SpecError(f"[run] python does not exist: {python}")
        return python
    if shutil.which(python) is None:
        raise SpecError(f"[run] python not found on PATH: {python}")
    return python


def _reject_unknown(table: dict, known: frozenset[str], where: str) -> None:
    """Refuse keys a table does not define, naming the ones it does."""
    extra = sorted(set(table) - known)
    if extra:
        raise SpecError(f"{where}: unknown key(s) {', '.join(extra)}; "
                        f"known: {', '.join(sorted(known))}")


def _reject_ignored_targets(mutants: Sequence[Mutant], ignore: Sequence[str]) -> None:
    """Refuse an edit whose file the sandbox copy would leave out.

    Ignore patterns match each path component, as ``shutil.ignore_patterns``
    does, so an edit under an ignored directory is caught here at load time
    instead of surfacing as an unexplained STALE for a file that exists.
    """
    for mutant in mutants:
        for edit in mutant.edits:
            for part in Path(edit.file).parts:
                for pattern in ignore:
                    if fnmatch.fnmatch(part, pattern):
                        raise SpecError(
                            f"mutant {mutant.name!r}: {edit.file} is excluded "
                            f"from the sandbox by ignore pattern {pattern!r}")


def _inside(path: Path, other: Path) -> bool:
    """Whether ``path`` is ``other`` or a path below it.

    ``Path.is_relative_to`` says this in one call, but only from Python 3.9,
    and this tool runs on the floor version it claims to support.
    """
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


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
    _reject_unknown(raw, STAGE_KEYS, "[stage]")
    file = raw.get("file")
    env = raw.get("env")
    as_path = raw.get("as")
    if not isinstance(file, str) or not file:
        raise SpecError("[stage] file must name the file the suite reads")
    if not isinstance(env, str) or not env:
        raise SpecError("[stage] env must name the variable the suite reads")
    if "=" in env or "\0" in env:
        raise SpecError(f"[stage] env {env!r} is not a variable name")
    if as_path is not None and (not isinstance(as_path, str) or not as_path
                                or os.path.isabs(as_path)
                                or ".." in Path(as_path).parts
                                or as_path.endswith(("/", os.sep))
                                or os.path.normpath(as_path) == "."):
        raise SpecError("[stage] `as` must name a file inside the temp directory: "
                        "a relative path, no `..`, not a directory")
    path = Path(file).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise SpecError(f"[stage] file does not exist: {path}")
    try:
        source = _read_text(path, str(path))
    except _Unreadable as exc:
        raise SpecError(f"[stage] {exc}") from None
    return Stage(file=path.resolve(), env=env, as_path=as_path, source=source)


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
    _reject_unknown(raw, MUTANT_KEYS, where)

    inline = {k: raw[k] for k in ("file", "find", "replace") if k in raw}
    listed = raw.get("edit")
    if inline and listed is not None:
        raise SpecError(f"{where}: use file/find/replace or [[mutant.edit]], not both")
    if listed is None:
        if not inline:
            raise SpecError(f"{where}: needs file/find/replace, or a [[mutant.edit]] list")
        listed = [inline]
    if not isinstance(listed, list):
        raise SpecError(f"{where}: edit must be a list of [[mutant.edit]] tables")
    if not listed:
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
    _reject_unknown(raw, EDIT_KEYS, where)
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
    if file.endswith(("/", os.sep)) or os.path.normpath(file) == ".":
        raise SpecError(f"{where}: file must name a file, not a directory")
    # One spelling per path, so 'a.py' and './a.py' in one mutant are one
    # target and the second edit sees the first edit's result.
    return Edit(file=Path(os.path.normpath(file)).as_posix(), find=find,
                replace=replace)


# --- sandbox ---------------------------------------------------------------


def apply_edits(edits: Iterable[Edit],
                read: Callable[[str], str],
                write: Callable[[str, str], None]) -> str | None:
    """Apply every edit exactly once, or return why the mutant is stale.

    All anchors are checked before anything is written, so a mutant whose
    second edit is stale leaves no half-applied first edit behind. ``read``
    raises ``_Unreadable`` for a target that cannot be edited.
    """
    texts: dict[str, str] = {}
    for edit in edits:
        if edit.file not in texts:
            try:
                texts[edit.file] = read(edit.file)
            except _Unreadable as exc:
                return str(exc)
        text = texts[edit.file]
        find, replace = edit.find, edit.replace
        count = _occurrences(text, find)
        if "\n" in find and "\r\n" in text and _occurrences(text, _crlf(find)) == 1:
            # TOML strings carry LF. A CRLF file takes the anchor in its own
            # line endings, so the sandbox differs by the mutant alone. This
            # is preferred over an LF match, which would write a lone LF into
            # a file that uses CRLF throughout.
            find, replace = _crlf(find), _crlf(replace)
        count = _occurrences(text, find)
        if count != 1:
            return _stale_reason(count, edit, text)
        texts[edit.file] = text.replace(find, replace, 1)
    for file, text in texts.items():
        write(file, text)
    return None


def _occurrences(text: str, find: str) -> int:
    """How many times ``find`` occurs in ``text``, overlapping ones included.

    ``str.count`` skips overlaps, so '--' would count once in '---' and an
    ambiguous anchor would pass the exactly-once rule.
    """
    count, at = 0, text.find(find)
    while at != -1:
        count += 1
        at = text.find(find, at + 1)
    return count


def _crlf(text: str) -> str:
    """``text`` with every line ending written as CRLF."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _lf(text: str) -> str:
    """``text`` with every line ending written as LF."""
    return text.replace("\r\n", "\n")


def _stale_reason(count: int, edit: Edit, text: str) -> str:
    """Why an edit could not be applied, naming line endings when they are why.

    A whitespace-flattened excerpt looks like text that is plainly in the
    file, so a file mixing CRLF and LF has to say so itself.
    """
    if count == 0 and _occurrences(_lf(text), _lf(edit.find)) == 1:
        return (f"anchor matches {edit.file} only with line endings normalised, so "
                f"that file mixes CRLF and LF: {_excerpt(edit.find)}")
    return f"anchor appears {count}x in {edit.file}: {_excerpt(edit.find)}"


def _excerpt(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[:limit - 3] + "...")


def _read_text(path: Path, label: str) -> str:
    """UTF-8 text with line endings preserved, or ``_Unreadable``."""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError:
        raise _Unreadable(f"{label} is not UTF-8; mutt_check edits UTF-8 text only")
    except OSError:
        raise _Unreadable(f"cannot read {label}")


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read_under(work: Path, root: Path | None = None) -> Callable[[str], str]:
    """Reader for copy mode that refuses to reach outside the sandbox.

    The tree is copied with symlinks kept as symlinks, so a symlinked file
    or directory would otherwise be written through to the real target. With
    ``root`` given, a target the copy left out is named as excluded rather
    than as unreadable, since it is plainly there in the project.
    """
    inside = work.resolve()

    def read(file: str) -> str:
        path = work / file
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            if root is not None and (root / file).exists():
                raise _Unreadable(f"{file} is in the project but not in the sandbox, "
                                  "so an ignore pattern excluded it")
            raise _Unreadable(f"cannot read {file}")
        if not _inside(resolved, inside):
            raise _Unreadable(f"{file} resolves outside the sandbox through a "
                              "symlink; refusing to write through it")
        return _read_text(path, file)
    return read


def build_command(spec: Spec, suites: Sequence[str] | None) -> tuple[str, ...]:
    """The suite command for one run: the spec's, or unittest over ``suites``."""
    if spec.command:
        return spec.command
    # Every run starts from a fresh copy, so bytecode left by an earlier
    # mutant cannot be picked up. -B, with PYTHONDONTWRITEBYTECODE set for
    # every run, only keeps the suite from writing any into the sandbox.
    return (spec.python, "-B", "-m", "unittest", *(suites or spec.suites))


@dataclasses.dataclass(frozen=True)
class RunResult:
    """One run of the suite, or the reason it did not run.

    ``problem`` is None when the suite ran, ``stale`` when an edit could
    not be applied, ``broken`` when a mutated Python file no longer
    compiles, and ``timeout`` when the suite ran past ``[run] timeout``.
    ``leaked`` is set when the sandbox could not be removed afterwards.
    """

    completed: subprocess.CompletedProcess | None
    problem: str | None
    detail: str
    sandbox: Path
    leaked: bool = False


def run_once(spec: Spec, edits: Sequence[Edit],
             suites: Sequence[str] | None = None,
             keep: bool = False) -> RunResult:
    """Build a sandbox, apply ``edits``, and run the suite against it once.

    Copy mode copies the project and runs the suite inside the copy. Stage
    mode writes the staged file into a temp directory and runs the suite in
    the real project with ``[stage] env`` pointing at that copy. The sandbox
    is deleted afterwards unless ``keep`` is set. Infrastructure faults
    raise ``RunError`` and are never turned into a verdict.
    """
    if spec.stage is None:
        _refuse_temp_inside_project(spec)
    tmp = Path(tempfile.mkdtemp(prefix="mutt_check-"))
    try:
        result = _run_in(tmp, spec, edits, suites)
    except BaseException as exc:
        if keep and isinstance(exc, RunError):
            raise RunError(f"{exc} [sandbox kept at {tmp}]") from None
        if not keep:
            _remove_sandbox(tmp)
        raise
    if keep:
        return result
    return dataclasses.replace(result, leaked=not _remove_sandbox(tmp))


def _run_in(tmp: Path, spec: Spec, edits: Sequence[Edit],
            suites: Sequence[str] | None) -> RunResult:
    """The body of ``run_once``, given the temp directory it owns."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    originals: dict[str, str] = {}
    mutated: dict[str, str] = {}
    if spec.stage is None:
        work = tmp / "project"
        _copy_project(spec, work)
        edits = _one_spelling_per_file(work, edits)
        read_copy = _read_under(work, spec.root)

        def read(file: str) -> str:
            originals[file] = read_copy(file)
            return originals[file]

        def write(file: str, text: str) -> None:
            mutated[file] = text
            _write_sandboxed(work / file, text)

        cwd = work
        # Put the copy first on sys.path explicitly. `python -m` adds the cwd
        # only when PYTHONSAFEPATH is unset, and a PYTHONPATH naming the real
        # tree would otherwise let the suite import the unmutated code.
        env["PYTHONPATH"] = _sandbox_pythonpath(work, spec.root, env.get("PYTHONPATH", ""))
    else:
        stage = spec.stage
        staged = tmp / (stage.as_path or stage.file.name)
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            _write_text(staged, stage.source)
        except (OSError, ValueError) as exc:
            raise RunError(f"cannot stage {stage.file.name} at {staged}: {exc}")
        edits = [dataclasses.replace(e, file=stage.file.name) for e in edits]

        def read(file: str) -> str:
            originals[file] = stage.source
            return stage.source

        def write(file: str, text: str) -> None:
            mutated[file] = text
            _write_text(staged, text)

        cwd = spec.root
        env[stage.env] = str(tmp if stage.as_path else staged)

    stale = apply_edits(edits, read, write)
    if stale is not None:
        return RunResult(None, "stale", stale, tmp)
    for file, text in mutated.items():
        broken = _compile_error(file, originals[file], text, spec.python)
        if broken:
            return RunResult(None, "broken", broken, tmp)
    completed = _run_suite(build_command(spec, suites), cwd, env, spec.timeout)
    if completed is None:
        return RunResult(None, "timeout", f"timed out after {spec.timeout:g}s", tmp)
    return RunResult(completed, None, "", tmp)


def _refuse_temp_inside_project(spec: Spec) -> None:
    """Refuse a temp directory the copy would walk into.

    Checked before anything is created, so a refused run leaves nothing in
    the project. A temp directory the ignore patterns exclude is fine: the
    copy never descends into it.
    """
    temp = Path(tempfile.gettempdir()).resolve()
    root = spec.root.resolve()
    if not _inside(temp, root):
        return
    inside = temp.relative_to(root)
    if any(fnmatch.fnmatch(part, pattern)
           for part in inside.parts for pattern in spec.ignore):
        return
    raise RunError(f"the temp directory {temp} is inside the project {root}, so the "
                   "copy would include itself; set TMPDIR outside the project, or add "
                   f"{inside.parts[0]!r} to [run] ignore")


def _spec_link(spec: Spec) -> frozenset[str]:
    """The spec's own path inside the project, which the sandbox never reads.

    A spec symlinked into the tree it exercises is a supported layout, so
    that one link must not count as a link out of the project.
    """
    if spec.spec_file is None:
        return frozenset()
    # Resolve the directory but not the spec itself: the spec may BE the link.
    named = spec.spec_file.parent.resolve() / spec.spec_file.name
    try:
        return frozenset({str(named.relative_to(spec.root.resolve()))})
    except ValueError:
        return frozenset()


def _copy_project(spec: Spec, work: Path) -> None:
    """Copy the project into ``work``, refusing a copy that would not isolate.

    Raises RunError when an entry cannot be copied, or when a symlink in the
    copy still reaches outside it.
    """
    try:
        shutil.copytree(spec.root, work, symlinks=True,
                        ignore=shutil.ignore_patterns(*spec.ignore))
    except shutil.Error as exc:
        entries = exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
        prefix = str(spec.root) + os.sep
        lines = [f"{_relative(entry[0], spec.root)}: {str(entry[2]).replace(prefix, '')}"
                 for entry in entries if isinstance(entry, tuple) and len(entry) == 3]
        raise RunError("could not copy the project into the sandbox: "
                       + _first_five(lines or [str(exc)])
                       + ". Add unreadable or special files to [run] ignore")
    except (OSError, RecursionError) as exc:
        raise RunError("could not copy the project into the sandbox: "
                       f"{type(exc).__name__}: {exc}")
    escaping = _escaping_links(work, _spec_link(spec))
    if escaping:
        raise RunError("symlinks in the project point outside it, so the suite "
                       "would write through them and import the real code: "
                       + _first_five(escaping)
                       + ". Add them to [run] ignore, or replace them with copies")


def _sandbox_pythonpath(work: Path, root: Path, current: str) -> str:
    """PYTHONPATH for the run: the copy first, project entries remapped into it.

    An entry naming the project, or a directory inside it, would let the
    suite import the unmutated code, which reports every mutant as
    surviving. Such an entry is rewritten to its place in the copy.
    """
    root = root.resolve()
    entries = [str(work)]
    for entry in current.split(os.pathsep) if current else []:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            entries.append(entry)
            continue
        if resolved == root:
            continue
        entries.append(str(work / resolved.relative_to(root))
                       if _inside(resolved, root) else entry)
    return os.pathsep.join(entries)


def _first_five(items: Sequence[str]) -> str:
    """Up to five items joined by '; ', with a count of the rest."""
    more = f" (and {len(items) - 5} more)" if len(items) > 5 else ""
    return "; ".join(items[:5]) + more


def _relative(path: str, root: Path) -> str:
    """``path`` relative to ``root`` when it is under it, else unchanged."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


def _escaping_links(work: Path, skip: frozenset[str] = frozenset()) -> list[str]:
    """Symlinks in the copy that resolve outside it, as 'link -> target'.

    The copy keeps links as links, so an absolute link still reaches the
    real filesystem and a relative link that leaves the project dangles. A
    link whose resolution fails counts as escaping; on Python 3.12 and
    earlier that includes a symlink loop, which later versions resolve to
    the link itself and so treat as inside. ``skip`` names links that are
    allowed to leave, given as paths relative to the copy.
    """
    inside = work.resolve()
    found = []
    for dirpath, dirnames, filenames in os.walk(work):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if not path.is_symlink() or str(path.relative_to(work)) in skip:
                continue
            try:
                escapes = not _inside(path.resolve(), inside)
            except (OSError, RuntimeError):
                escapes = True
            if escapes:
                found.append(f"{path.relative_to(work)} -> {os.readlink(path)}")
    return found


def _one_spelling_per_file(work: Path, edits: Sequence[Edit]) -> list[Edit]:
    """Give edits that reach the same file one spelling, so they compose.

    Paths are normalised at load time. This catches what normalising cannot,
    such as 'A.py' and 'a.py' on a case-insensitive filesystem, by comparing
    the files the copy actually holds.
    """
    seen: dict[tuple[int, int], str] = {}
    merged = []
    for edit in edits:
        try:
            info = (work / edit.file).stat()
        except OSError:
            merged.append(edit)
            continue
        name = seen.setdefault((info.st_dev, info.st_ino), edit.file)
        merged.append(dataclasses.replace(edit, file=name))
    return merged


def _write_sandboxed(path: Path, text: str) -> None:
    """Write a mutated file in the copy, even one the project keeps read-only.

    ``copytree`` preserves modes, so a read-only file is read-only in the
    copy too. The copy is disposable, so making it writable there changes
    nothing real.
    """
    try:
        mode = path.stat().st_mode
        if not mode & stat.S_IWUSR:
            path.chmod(mode | stat.S_IWUSR)
        _write_text(path, text)
    except OSError as exc:
        raise RunError(f"cannot write {path.name} in the sandbox: {exc}")


#: Suffixes that make a file Python source whatever its first line says.
PY_SUFFIXES = (".py", ".pyw", ".pyi")


def _is_python_source(name: str, text: str) -> bool:
    """Whether a target is Python source: by suffix, or by a python shebang.

    Classified positively, so a deployed hook with no extension is still
    judged and a data file a mutant breaks is not mistaken for code.
    """
    if name.endswith(PY_SUFFIXES):
        return True
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        return "python" in first
    # A file with no suffix at all, such as a deployed hook: the caller's
    # compile of the unmutated text is what actually decides, and a data
    # file that does not parse as Python is skipped there.
    return not Path(name).suffix


def _compiles_with(python: str, text: str) -> bool:
    """Whether ``python`` compiles ``text``; False when it cannot be asked.

    The suite runs under ``[run] python``, which may accept syntax the
    interpreter running mutt_check does not, so a refusal is confirmed with
    the interpreter that will actually import the file.
    """
    if not python or python == sys.executable:
        return False
    try:
        done = subprocess.run(
            [python, "-c", "import sys; compile(sys.stdin.read(), 'm', 'exec')"],
            input=text, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _compile_error(name: str, before: str, after: str,
                   python: str = "") -> str | None:
    """Why a mutated Python file no longer compiles, or None.

    Only a file whose unmutated text compiles is judged, so a project
    written for a newer Python than the one running mutt_check is never
    reported BROKEN for syntax this interpreter lacks; where the suite runs
    under a different interpreter, a refusal is confirmed with that one. A
    byte-order mark is stripped for the check alone: the sandbox still gets
    the file byte for byte.
    """
    before, after = before.lstrip("\ufeff"), after.lstrip("\ufeff")
    if not _is_python_source(name, before):
        return None
    try:
        compile(before, name, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    try:
        compile(after, name, "exec", dont_inherit=True)
    except SyntaxError as exc:
        if _compiles_with(python, after):
            return None
        return f"mutant does not compile: {exc.msg} ({name}, line {exc.lineno})"
    except ValueError as exc:
        if _compiles_with(python, after):
            return None
        return f"mutant does not compile: {exc} ({name})"
    return None


def _run_suite(command: Sequence[str], cwd: Path, env: dict[str, str],
               timeout: float | None) -> subprocess.CompletedProcess | None:
    """Run the suite in its own process group. None means it timed out.

    Output goes to unnamed temp files rather than pipes, so a process the
    suite started and never reaped cannot hold the run open waiting for
    end-of-file. When the suite exits or times out, its whole process group
    is killed, so nothing it started outlives the sandbox.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=out,
                                    stderr=err, start_new_session=True)
        except OSError as exc:
            raise RunError(f"cannot run {command[0]}: {exc}")
        returncode: int | None = None
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        finally:
            _kill_group(proc)
        if returncode is None:
            return None
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            list(command), returncode,
            out.read().decode("utf-8", "replace"),
            err.read().decode("utf-8", "replace"))


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill every process left in the suite's group, then reap the suite."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _remove_sandbox(tmp: Path) -> bool:
    """Delete a sandbox, including directories the copy kept read-only.

    Returns False, after saying why on stderr, when it cannot be removed.
    """
    for dirpath, dirnames, _files in os.walk(tmp):
        for name in dirnames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                try:
                    path.chmod(path.stat().st_mode | stat.S_IRWXU)
                except OSError:
                    pass
    try:
        shutil.rmtree(tmp)
    except OSError as exc:
        print(f"mutt_check: could not remove sandbox {tmp}: {exc}", file=sys.stderr)
        return False
    return True


# --- verdicts --------------------------------------------------------------

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
_SKIP_RE = re.compile(r"skipped=(\d+)|(\d+) skipped")
_FAILED_RE = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)
_EXCEPTION_RE = re.compile(r"^\w+(?:Error|Exception|Exit)\b.*$", re.MULTILINE)
#: unittest reports a test module that failed to import as a synthetic test.
_LOAD_FAILED = "unittest.loader._FailedTest"
_SEPARATOR = "\n" + "=" * 70


def _output(completed: subprocess.CompletedProcess) -> str:
    """stdout, then stderr, where unittest writes its closing summary.

    With stderr last, the runner's own summary follows anything the tests
    themselves printed.
    """
    return (completed.stdout or "") + "\n" + (completed.stderr or "")


def _clip(text: str, limit: int = 120) -> str:
    """``text`` cut to ``limit`` characters, marked when cut."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _tail(completed: subprocess.CompletedProcess) -> str:
    """The last non-blank line of output, for the detail column."""
    lines = [ln.strip() for ln in _output(completed).splitlines() if ln.strip()]
    return _clip(lines[-1] if lines else f"exit {completed.returncode}, no output")


def _last_ran(text: str) -> re.Match[str] | None:
    """unittest's closing 'Ran N tests' line, which is the last one printed.

    A test that prints unittest-style text would otherwise have the run
    judged by what it printed rather than by the runner's summary.
    """
    last = None
    for last in _RAN_RE.finditer(text):
        pass
    return last


def _tests_ran(text: str) -> int | None:
    """How many tests unittest reports running, or None without its summary."""
    match = _last_ran(text)
    return int(match.group(1)) if match else None


def _skipped(text: str) -> int:
    """Skipped tests, read only from the summary after the last 'Ran' line."""
    match = _last_ran(text)
    return sum(int(a or b) for a, b in _SKIP_RE.findall(
        text[match.end():] if match else text))


#: unittest's own frames, which appear however the runner was spelled.
_UNITTEST_FRAME_RE = re.compile(r"unittest[/\\](?:loader|main|suite)\.py")


def _load_failure(text: str, returncode: int) -> str | None:
    """Why the suite could not deliver a verdict, or None when it could.

    Every sign is read out of the output, so how the runner was spelled on
    the command line does not matter. unittest reports a module it could not
    import as a synthetic ``_FailedTest``; a summary saying zero tests ran
    delivered no verdict either; and unittest given a module by name lets
    anything other than an ImportError escape while importing it, leaving a
    traceback through unittest's own frames and no summary at all.
    """
    start = text.find(_LOAD_FAILED)
    # unittest prints its own separator above the block, which text a test
    # printed does not have, so a red run cannot be faked into BROKEN.
    if returncode != 0 and start != -1 and _SEPARATOR in text[:start]:
        ends = [at for at in (text.find(_SEPARATOR, start),) if at != -1]
        ran = _last_ran(text)
        if ran and ran.start() > start:
            ends.append(ran.start())
        found = _EXCEPTION_RE.findall(text[start:min(ends)] if ends else text[start:])
        return _clip(found[-1]) if found else "a test module failed to import"
    ran_count = _tests_ran(text)
    if ran_count == 0:
        return "0 tests ran"
    if ran_count is None and returncode != 0 and _UNITTEST_FRAME_RE.search(text):
        found = _EXCEPTION_RE.findall(text)
        return _clip(found[-1]) if found else "the runner exited before any test ran"
    return None


def _all_suites(spec: Spec, mutants: Sequence[Mutant]) -> tuple[str, ...] | None:
    """Every suite this run can judge a mutant against, overrides included.

    None when a custom command runs the suite, which takes no suite names.
    """
    if spec.command:
        return None
    listed = list(spec.suites)
    for mutant in mutants:
        for suite in mutant.suites or ():
            # A class inside a module already listed is already covered, and
            # naming both would run those tests twice in the control.
            if suite not in listed and not any(
                    suite.startswith(f"{other}.") for other in listed):
                listed.append(suite)
    return tuple(listed)


def control_verdict(spec: Spec, mutants: Sequence[Mutant] = (),
                    keep: bool = False) -> Verdict:
    """Run the unmutated tree once. Anything but a clean green run is RED.

    RED covers a failing suite, one that could not deliver a verdict, one
    that ran past the timeout, and one that skipped tests, since a skipped
    test can catch nothing. Every suite the given mutants name is run here
    too: a mutant judged against a suite the control never ran could be
    reported caught by a suite that was already red.
    """
    result = run_once(spec, (), _all_suites(spec, mutants), keep=keep)
    where = str(result.sandbox) if keep else None

    def verdict(outcome: str, detail: str) -> Verdict:
        return Verdict(CONTROL_NAME, outcome, detail, where, result.leaked)

    if result.completed is None:
        return verdict("red", result.detail)
    completed = result.completed
    text = _output(completed)
    failure = _load_failure(text, completed.returncode)
    if failure or completed.returncode != 0:
        return verdict("red", failure or _tail(completed))
    skipped = _skipped(text)
    if skipped and not spec.allow_skips:
        return verdict("red", f"{skipped} test(s) skipped; a skipped test can catch "
                              "nothing. Fix them or set allow_skips = true")
    ran = _tests_ran(text)
    detail = f"{ran} tests" if ran is not None else _tail(completed)
    return verdict("green", detail + (f", {skipped} skipped" if skipped else ""))


def mutant_verdict(spec: Spec, mutant: Mutant, keep: bool = False) -> Verdict:
    """Apply one mutant and judge it: caught, survived, stale, or broken."""
    result = run_once(spec, mutant.edits, mutant.suites, keep=keep)
    where = str(result.sandbox) if keep else None

    def verdict(outcome: str, detail: str = "") -> Verdict:
        return Verdict(mutant.name, outcome, detail, where, result.leaked)

    if result.problem == "stale":
        return verdict("stale", result.detail)
    if result.problem in ("broken", "timeout"):
        return verdict("broken", result.detail)
    completed = result.completed
    assert completed is not None
    text = _output(completed)
    failure = _load_failure(text, completed.returncode)
    if failure:
        return verdict("broken", failure)
    if completed.returncode == 0:
        return verdict("survived")
    failed = _FAILED_RE.findall(text)
    return verdict("caught", ", ".join(dict.fromkeys(failed)) if failed else _tail(completed))


@dataclasses.dataclass
class Report:
    """The whole run: the control, each mutant in order, and the exit code."""

    control: Verdict
    mutants: list[Verdict]
    #: How many mutants this run covered: all of the spec's, or the ones
    #: named by --only. The denominator of the summary line.
    selected: int

    def _names(self, outcome: str) -> list[str]:
        return [v.name for v in self.mutants if v.outcome == outcome]

    @property
    def applied(self) -> int:
        return sum(1 for v in self.mutants if v.outcome != "stale")

    @property
    def survived(self) -> list[str]:
        return self._names("survived")

    @property
    def stale(self) -> list[str]:
        return self._names("stale")

    @property
    def broken(self) -> list[str]:
        return self._names("broken")

    @property
    def leaked_sandboxes(self) -> int:
        """Sandboxes this run could not remove, the control's included."""
        return sum(1 for v in [self.control, *self.mutants] if v.leaked)

    @property
    def exit_code(self) -> int:
        if self.control.outcome != "green":
            return EXIT_UNUSABLE
        if self.survived or self.stale or self.broken:
            return EXIT_UNPINNED
        return EXIT_PINNED

    def summary(self) -> str:
        """One line a reader can quote: counts from this run, not the spec."""
        leaks = (f"; {self.leaked_sandboxes} sandbox(es) could not be removed, "
                 "see stderr" if self.leaked_sandboxes else "")
        if self.control.outcome != "green":
            return ("control RED: no mutant verdict would mean anything. "
                    f"{self.control.detail}{leaks}")
        line = (f"{self.applied} of {self.selected} mutants applied, control "
                f"green, {len(self.survived)} survived, {len(self.stale)} stale, "
                f"{len(self.broken)} broken")
        names = self.survived + self.stale + self.broken
        return line + (f": {', '.join(names)}" if names else "") + leaks

    def as_json(self) -> dict:
        return {
            "control": dataclasses.asdict(self.control),
            "mutants": [dataclasses.asdict(v) for v in self.mutants],
            "selected": self.selected,
            "applied": self.applied,
            "survived": self.survived,
            "stale": self.stale,
            "broken": self.broken,
            "leaked_sandboxes": self.leaked_sandboxes,
            "exit_code": self.exit_code,
        }


def select_mutants(spec: Spec, only: Sequence[str] = ()) -> list[Mutant]:
    """The mutants a run covers: every one, or those named by ``--only``.

    An unknown name is a SpecError rather than a filter that matches
    nothing, so a typo cannot turn into a run that checked nothing.
    """
    unknown = set(only) - {m.name for m in spec.mutants}
    if unknown:
        raise SpecError(f"no such mutant: {', '.join(sorted(unknown))}")
    return [m for m in spec.mutants if not only or m.name in only]


def check(spec: Spec, only: Sequence[str] = (), keep: bool = False,
          emit: Callable[[Verdict], None] | None = None) -> Report:
    """Run the control and then every selected mutant.

    ``emit`` receives each verdict as it lands, so a long run shows
    progress. A red control stops the run before any mutant is applied.
    """
    selected = select_mutants(spec, only)
    notify = emit or (lambda _v: None)
    control = control_verdict(spec, selected, keep=keep)
    notify(control)
    report = Report(control=control, mutants=[], selected=len(selected))
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
             "survived": "SURVIVED", "stale": "STALE", "broken": "BROKEN"}[v.outcome]
    line = f"  {v.name:<{width}}  {label:<8}"
    if v.detail:
        line += f"  {v.detail}"
    if v.sandbox:
        line += f"  [{v.sandbox}]"
    return line.rstrip()


def list_mutants(spec: Spec, mutants: Sequence[Mutant] | None = None) -> str:
    """Mutants as a reader would want them: name, files, and why.

    ``mutants`` defaults to every mutant in the spec.
    """
    lines = []
    for m in (spec.mutants if mutants is None else mutants):
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
        prog="mutt_check",
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
            print(list_mutants(spec, select_mutants(spec, args.only)))
            return EXIT_PINNED
        width = max([len(CONTROL_NAME)] + [len(m.name) for m in spec.mutants])
        emit = None if args.json else (
            lambda v: print(format_verdict(v, width), flush=True))
        report = check(spec, only=args.only, keep=args.keep, emit=emit)
    except (SpecError, RunError) as exc:
        print(f"mutt_check: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.json:
        print(json.dumps(report.as_json(), indent=2))
    else:
        print()
        print(report.summary())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
