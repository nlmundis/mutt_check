# mutcheck

Prove a test suite catches the defects it claims to pin.

A green suite is evidence that nothing the suite checks is broken. It is not
evidence that any particular design decision is pinned down, because a
decision no test reaches can be reverted with every test still green.
mutcheck takes a curated list of mutants, each one reverting one load-bearing
decision in the code under test, applies them one at a time to a throwaway
copy of the project, and requires the suite to go red for every one.

```
$ mutcheck
  control            green     2 tests
  lowercase_dropped  caught    test_lowercases
  edges_not_trimmed  caught    test_strips_leading_and_trailing_separators
  collapse_dropped   SURVIVED

3 of 3 mutants applied, control green, 1 survived, 0 stale, 0 broken: collapse_dropped
```

That run is [`examples/slugify`](examples/slugify). The suite is green. The
function collapses runs of separators into one hyphen, the docstring says so,
and no test checks it. `collapse_dropped` reverts that decision and the suite
does not notice. That is the gap this tool exists to show, and the exit code
is 1 until a test closes it.

Single file, standard library only, Python 3.11 or newer.

## How this differs from mutation testing tools

Tools like mutmut and cosmic-ray generate mutants by flipping operators and
constants across the whole codebase, then report a kill rate. mutcheck does
something narrower on purpose:

- **The mutants are curated.** Each one is a design decision the code makes,
  written down by name, with the reason the decision exists. The spec doubles
  as an executable list of what the suite is supposed to hold. A reader can
  learn the module's load-bearing choices from the spec alone.
- **The control run is mandatory.** The unmutated tree runs first and must be
  green. A suite that is already red reports every mutant as caught,
  including a no-op, so nothing is judged until the control passes.
- **A missing anchor is a failure, not a skip.** When a refactor moves the
  text a mutant anchors on, the mutant is STALE and the run exits 1. A check
  that rotted tested nothing, and reporting that as success is the one outcome
  the tool must never produce.
- **An anchor must appear exactly once.** First-occurrence matching that
  happens to hit the right line is one refactor away from retargeting
  silently. Two occurrences is STALE too; widen the anchor to include the
  decision's own neighbouring line.
- **A mutant the suite could not load is BROKEN, not caught.** A replace
  with a typo turns every test module that imports the file into an import
  error. That is a red suite, but it says nothing about whether the decision
  is pinned, so it fails the run under its own name.
- **Mutants can target a file the suite reads from outside the tree**, such
  as a deployed hook or a config the tests locate through an environment
  variable. The file is staged in a temp directory and the suite is pointed
  at the copy, so the deployed original is never touched.

It is also cheap. A dozen curated mutants run in seconds, and the output is
readable in a pull request without a report viewer.

## Install

```bash
pip install .
```

Or copy `mutcheck.py` into the repo and run it with `python mutcheck.py`. It
has no dependencies.

## The spec

mutcheck reads `mutcheck.toml` from the current directory, or the path given
as its first argument. The project root is the spec's directory unless
`--root` says otherwise.

```toml
[run]
suites = ["tests.test_slugify"]      # python -B -m unittest <suites>
# command = ["pytest", "-q"]         # any command; non-zero exit is red
# python = "venv/bin/python"         # interpreter for the suites; default: the one running mutcheck
# ignore = ["fixtures", "scratch"]   # extra copytree ignore patterns
# ignore_defaults = true             # false: copy .git, venv and caches too (see below)
# allow_skips = false                # a skipping control is red unless this is true
# timeout = 120                      # seconds per suite run; a run that exceeds it is BROKEN

[[mutant]]
name = "lowercase_dropped"
why = "Case folding is the contract; 'Hello' and 'hello' must slug the same."
file = "slugify.py"
find = "text = text.lower()"
replace = "text = text"
```

`find` must be non-empty and must appear exactly once in `file`. `replace`
may be empty, which deletes the anchor. A mutant whose `find` equals its
`replace` is rejected at load time, since it could never be caught. `python`
is checked at load time too: a path that does not exist, or a bare name not
on `PATH`, is a spec error rather than a traceback halfway through a run.

By default the sandbox omits `.git`, `.hg`, `.svn`, `venv`, `.venv`,
`__pycache__`, `*.pyc` and the `.mypy_cache`, `.pytest_cache`, `.ruff_cache`,
`.tox` and `.nox` directories. A suite that shells out to `git` will go
control-red in copy mode for that reason; set `ignore_defaults = false` and
supply your own `ignore` list to copy the history in.

### Several edits in one mutant

Some decisions are held by two lines, or by two files. A mutant may carry a
list of edits; every anchor is checked before any is applied, so a mutant
with one stale edit applies none of them and is reported STALE as a whole.

```toml
[[mutant]]
name = "bom_defeats_the_anchor__both_defenses_removed"
[[mutant.edit]]
file = "list_commands.py"
find = '\\A\\ufeff?---'
replace = '\\A---'
[[mutant.edit]]
file = "list_commands.py"
find = 'encoding="utf-8-sig"'
replace = 'encoding="utf-8"'
```

### A different suite for one mutant

When the file under test contains its own guard (a source scan, say), a
mutant judged against the whole suite may be caught by that guard rather
than by the tests the mutant is about. Judge it against the suites that make
the verdict mean what it says:

```toml
[[mutant]]
name = "fixture_loses_its_branch"
suites = ["tests.test_commit_memory_store"]
file = "tests/fixtures.py"
find = '"-b", "main",'
replace = ''
```

This needs the default unittest runner; it is rejected alongside a custom
`command`.

### A target outside the tree

Add a `[stage]` table when the suite reads the file under test through an
environment variable rather than from the project. Every mutant then edits a
copy of that file, and `file` is omitted from the edits.

```toml
[stage]
file = "~/.claude/hooks/branch_owner_reminder.py"
env = "HOME"
as = ".claude/hooks/branch_owner_reminder.py"
```

With `as`, the copy lands at `<tmp>/<as>` and `env` is set to `<tmp>`, which
suits a suite that resolves the file from a root such as `$HOME`. Without
`as`, `env` is set to the path of the copy itself. In stage mode the suite
runs in the real project directory, not a copy, because the project is not
what is being mutated; whatever the suite writes to its working directory
lands there as it would under any other runner.

Redirecting `HOME` replaces it for the whole run, so the suite and the
interpreter lose git config, tool caches and anything else they read from
there. Give `python` an absolute path in that configuration, and prefer a
purpose-built variable when the suite can read one.

## Verdicts and exit codes

| Line | Meaning |
|---|---|
| `control green` | The unmutated tree passed. Mutant verdicts below mean something. |
| `control RED` | The unmutated tree failed, or skipped tests. Nothing else runs. Exit 2. |
| `caught` | The suite went red with the mutant applied. The named tests are the ones that failed. |
| `SURVIVED` | The suite stayed green. Nothing pins that decision. Exit 1. |
| `STALE` | The anchor was not found exactly once, or the target could not be edited (not UTF-8, or a symlink out of the sandbox). The mutant tested nothing. Exit 1. |
| `BROKEN` | The suite ran but could not deliver a verdict: a test module failed to import, zero tests ran, or the run hit `timeout`. Rewrite the mutant so the code still loads. Exit 1. |

The closing line counts what this run did, not what the spec declares:
`2 of 3 mutants applied` means one was stale. Quote that line, not a number
from memory.

| Exit | When |
|---|---|
| 0 | Control green and every mutant caught. |
| 1 | At least one mutant survived, went stale, or was broken. |
| 2 | Control red, spec invalid, unknown `--only` name, or the sandbox or interpreter could not be started. |

BROKEN detection reads unittest's output, under the default runner or a
`command` that contains `unittest`. Under any other runner an import
failure is whatever that runner makes of it, which is usually a red run
reported as `caught`; keep unittest where that distinction matters.

## Command line

```
mutcheck [spec] [--root DIR] [--only NAME ...] [--list] [--json] [--keep]
```

- `--only NAME` runs one mutant (repeatable). Handy for re-running a survivor
  after adding the test that should kill it. An unknown name exits 2 rather
  than running nothing.
- `--list` prints the mutants with their `why` and runs nothing.
- `--json` prints the full report as JSON, for a CI step to read.
- `--keep` leaves every sandbox on disk and prints its path, so a surviving
  mutant can be inspected as the suite saw it. Each sandbox is a full copy of
  the tree and nothing removes them for you.

## Writing mutants that mean something

- **Revert a decision, not an operator.** `max` to `min` is a good mutant when
  the decision is "the newest wins". `+` to `-` at random is noise.
- **Name the decision, and say why it exists.** The name is what appears in the
  run and in the summary line; `why` is what a reader gets from `--list`. A
  year later the spec is the only place this knowledge is written down.
- **Anchor on the decision's own line.** When that line appears twice in the
  file, include the line before or after it in `find` so the anchor is unique.
  The tool refuses first-occurrence matching for a reason: one of the harnesses
  this tool was distilled from had a mutant that passed because a duplicated
  line moved and the first match landed on the wrong copy.
- **Add a mutant together with the test that kills it.** A known survivor in
  the spec is a to-do dressed as evidence. Keep those in the issue tracker.
- **One decision per mutant.** A mutant that reverts two decisions is caught
  when either one is pinned, which tells you nothing about the other.

## What the tool does for you

Things that went wrong in the hand-written harnesses this was distilled from,
now handled once:

- The control runs first, every time, and stops the run if red. A control
  that ran zero tests is red too.
- Anchors must appear exactly once; STALE fails the run.
- Every run starts from a fresh copy of the tree (or a fresh staged copy of the
  external file), so mutcheck never writes into the project, and mutants never
  see each other. A target that resolves outside the sandbox through a symlink
  is refused rather than written through.
- Line endings are preserved byte for byte, so the sandbox differs from the
  project by the mutant alone.
- The suite runs with `-B` and `PYTHONDONTWRITEBYTECODE=1`. CPython validates
  a cached `.pyc` against the source's size and its mtime truncated to whole
  seconds, so two mutants of equal length written within one second can run
  the previous mutant's bytecode and be reported as surviving when they were
  caught.
- A control with skipped tests is red by default. A skipped test can catch
  nothing, so it makes the suite weaker than its headline count suggests. Set
  `allow_skips = true` when the skips are deliberate.
- The summary line reports counts from the run, not the spec.

## Limits

- **Linux and macOS.** Windows is untested: the tool relies on symlink
  semantics and POSIX path handling, and CI runs on Ubuntu only.
- **UTF-8 source only.** A target in another encoding is reported STALE
  with that reason rather than edited.
- **The sandbox is imported through the working directory.** The suite
  runs with the sandbox as its cwd, which is where `python -m unittest` puts
  the project on `sys.path`. A project that is importable only through an
  installed distribution (a `src/` layout under `pip install -e .`, or a
  non-editable install into the venv named by `python`) keeps importing the
  installed code, and every mutant survives. Use a flat layout, or a
  `command` that installs the sandbox first.
- **Every run copies the tree.** That is the isolation, and it is fine for a
  few dozen mutants over a repository of ordinary size. Trim the copy with
  `ignore` when it is not.

## Checking mutcheck itself

```bash
make check
```

That runs the unit suite and then mutcheck against its own suite using the
[`mutcheck.toml`](mutcheck.toml) in this repository, which reverts each of
the rules above one at a time. Both are offline, touch nothing outside a
temp directory, and finish in a few seconds.

## License

MIT. See [LICENSE](LICENSE).
