# mutt_check

Prove a test suite catches the defects it claims to pin.

A green suite is evidence that nothing the suite checks is broken. It is not
evidence that any particular design decision is pinned down, because a
decision no test reaches can be reverted with every test still green.
mutt_check takes a curated list of mutants, each one reverting one load-bearing
decision in the code under test, applies them one at a time to a throwaway
copy of the project, and requires the suite to go red for every one.

```
$ mutt_check
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

Single file, Python 3.9 or newer. Standard library only, except `tomli` on
3.10 and earlier, where the standard library has no `tomllib`.

## How this differs from mutation testing tools

Tools like mutmut and cosmic-ray generate mutants by flipping operators and
constants across the whole codebase, then report a kill rate. mutt_check does
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
  error, and a mutated file that no longer compiles is refused before the
  suite even runs. That is a red suite, but it says nothing about whether the decision
  is pinned, so it fails the run under its own name.
- **Mutants can target a file the suite reads from outside the tree**, such
  as a deployed hook or a config the tests locate through an environment
  variable. The file is staged in a temp directory and the suite is pointed
  at the copy, so the deployed original is never touched.

Each mutant costs one run of the suite, so a dozen mutants over a fast suite
take seconds, and the output is readable in a pull request without a report
viewer.

## Install

```bash
pip install .
```

Or copy `mutt_check.py` into the repo and run it with `python mutt_check.py`. It
pulls in `tomli` only on Python 3.10 and earlier, where the standard library
has no `tomllib`.

## The spec

mutt_check reads `mutt_check.toml` from the current directory, or the path given
as its first argument. The project root is the spec's directory unless
`--root` says otherwise.

```toml
[run]
suites = ["tests.test_slugify"]      # python -B -m unittest <suites>
# command = ["pytest", "-q"]         # any command; non-zero exit is red
# python = "venv/bin/python"         # interpreter for the suites; rejected alongside `command`
# ignore = ["fixtures", "scratch"]   # extra copytree ignore patterns
# use_default_ignores = true             # false: copy .git, venv and caches too (see below)
# allow_skips = false                # a skipping control is red unless this is true
# timeout = 120                      # seconds per suite run; a mutant run over it is BROKEN, a control run RED

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
Unknown keys in any table are rejected, so a misspelled key is an error
rather than a setting silently ignored.

By default the sandbox omits `.git`, `.hg`, `.svn`, `venv`, `.venv`,
`__pycache__`, `*.pyc` and the `.mypy_cache`, `.pytest_cache`, `.ruff_cache`,
`.tox` and `.nox` directories. A suite that shells out to `git` will go
control-red in copy mode for that reason; set `use_default_ignores = false` and
supply your own `ignore` list to copy the history in. Patterns match one file
or directory name, as `shutil.ignore_patterns` does, so `tests/fixtures` is
rejected; write `fixtures`. An edit whose file sits under an ignored name is
rejected at load time. A file that cannot be copied (a FIFO, a socket, a file
without read permission) must be ignored, or the sandbox cannot be built.

### Several edits in one mutant

Some decisions are held by two lines, or by two files. A mutant may carry a
list of edits; every anchor is checked before any is applied, so a mutant
with one stale edit applies none of them and is reported STALE as a whole.

```toml
[[mutant]]
name = "bom_defeats_the_anchor__both_defenses_removed"
[[mutant.edit]]
file = "list_commands.py"
find = '\A\ufeff?---'
replace = '\A---'
[[mutant.edit]]
file = "list_commands.py"
find = 'encoding="utf-8-sig"'
replace = 'encoding="utf-8"'
```

Single-quoted TOML strings are literal: write backslashes exactly as they
appear in the source file.

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

The staged file is read once, when the spec loads, so the control and every
mutant judge the same content even if the real file changes during the run.
A staged file that is not UTF-8 is a spec error.

## Verdicts and exit codes

| Line | Meaning |
|---|---|
| `control green` | The unmutated tree passed. Mutant verdicts below mean something. |
| `control RED` | The unmutated tree failed, skipped tests, ran zero tests, or ran past `timeout`. Nothing else runs. Exit 2. |
| `caught` | The suite went red with the mutant applied. Under unittest the named tests are the ones that failed; under another runner the detail is a line of the output that reads like a summary, or the exit status when none does. |
| `SURVIVED` | The suite stayed green. Nothing pins that decision. Exit 1. |
| `STALE` | The anchor was not found exactly once, or the target could not be edited: it is not UTF-8, the file mixes line endings, or an ignore pattern kept it out of the copy. The mutant tested nothing. Exit 1. |
| `BROKEN` | The mutated file does not compile, or the suite ran but could not deliver a verdict: a test module failed to import, zero tests ran, or the run hit `timeout`. Rewrite the mutant so the code still loads. Exit 1. |

A symlink that leaves the project, a temp directory inside it, and a staged
file that cannot be written are all refused before the control runs, with
exit 2. In stage mode a target that is not UTF-8 is a spec error, also exit 2.

The closing line counts what this run did, not what the spec declares:
`2 of 3 mutants applied` means one was stale. Under `--only` the second number
is how many were selected. Quote that line, not a number
from memory.

| Exit | When |
|---|---|
| 0 | Control green and every mutant caught. |
| 1 | At least one mutant survived, went stale, or was broken. |
| 2 | Control red, spec invalid, unknown `--only` name, or the sandbox could not be built, staged, or started. |

A mutated Python file that no longer compiles is BROKEN under any runner. The
target counts as Python by its suffix, by a python shebang, or by having no
suffix at all, which covers a deployed hook; a byte-order mark does not hide
it. The check is skipped when the unmutated text does not compile either, so a
project written for a newer Python is never misjudged, and where `[run] python`
names another interpreter a refusal is confirmed with that one.

The other BROKEN rules read unittest's output, wherever it appears and however
the command was spelled: a test module unittest could not import, which it
reports as a synthetic `_FailedTest` under its own separator; a summary saying
zero tests ran; and a red run with no summary at all whose traceback passes
through unittest's own frames, which is what happens when a module given by
name raises something other than an ImportError while importing.

## Command line

```
mutt_check [spec] [--root DIR] [--only NAME ...] [--list] [--json] [--keep] [--version]
```

- `--only NAME` runs one mutant (repeatable). Handy for re-running a survivor
  after adding the test that should kill it. An unknown name exits 2 rather
  than running nothing.
- `--list` prints the mutants with their `why` and runs nothing. With `--only`
  it lists just those.
- `--json` prints the full report as JSON, for a CI step to read.
- `--keep` leaves every sandbox on disk and prints its path, so a surviving
  mutant can be inspected as the suite saw it. Each sandbox is a full copy of
  the tree in copy mode, or the staged file in stage mode, and nothing removes
  them for you.

The JSON report has one entry per verdict, in run order:

```json
{
  "control": {"name": "control", "outcome": "green", "detail": "2 tests", "sandbox": null, "leaked": false},
  "mutants": [{"name": "collapse_dropped", "outcome": "survived", "detail": "", "sandbox": null, "leaked": false}],
  "selected": 1, "applied": 1,
  "survived": ["collapse_dropped"], "stale": [], "broken": [],
  "leaked_sandboxes": 0,
  "exit_code": 1
}
```

## Driving it from an agent or CI

mutt_check is a plain command with machine-readable output, so anything that can
run a shell command in a checkout can drive it: an agent CLI, a CI job, a git
hook. It needs a filesystem, a Python 3.9 or newer interpreter, and permission
to start subprocesses, since running the suite is the whole point. A chat
surface with no shell cannot run it, and neither can a sandbox that forbids
subprocesses.

Read the result from the exit code first, then `--json` for the detail:
`survived`, `stale` and `broken` name the mutants, `selected` and `applied` are
the counts the summary line quotes, and `leaked_sandboxes` counts temp
directories that could not be removed.

```bash
mutt_check --json | python -c "import json,sys; print(json.load(sys.stdin)['survived'])"
```

An agent working inside this repository should read [AGENTS.md](AGENTS.md),
which names the commands and the rules a change here has to keep. Writing the
same file for your own project is how you tell an agent that a survived mutant
means write a test, and that a stale one means realign the anchor.

In CI, run it as one step and let the exit code fail the build. GitHub Actions
for this repository is in [.github/workflows/check.yml](.github/workflows/check.yml).
Bitbucket Pipelines:

```yaml
pipelines:
  default:
    - step:
        name: mutt_check
        image: python:3.12
        script:
          - pip install .
          - mutt_check
```

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
  external file), so mutt_check never writes into the project, and mutants never
  see each other. A project whose symlinks point outside it is refused, since
  the suite would write through them and import the real code.
- Line endings are preserved byte for byte, so the sandbox differs from the
  project by the mutant alone. A multi-line anchor written with LF in the spec
  matches a CRLF file in that file's own line endings.
- The copy is put first on `PYTHONPATH`, so the suite imports it even under
  `PYTHONSAFEPATH` or with the real tree on `PYTHONPATH`.
- The suite runs in its own process group with its output going to temp
  files, so a helper process it leaves behind cannot hold the run open, and
  the whole group is killed when the suite exits or times out.
- Every run starts from a fresh copy, so bytecode left by an earlier mutant can
  never be picked up. The default runner is invoked with `-B`, and every run
  has `PYTHONDONTWRITEBYTECODE=1`, so no bytecode is written into a sandbox
  either.
- A sandbox is removed even when the copy kept a directory read-only. One that
  still cannot be removed is counted in the summary line, not just on stderr.
- A control with skipped tests is red by default. A skipped test can catch
  nothing, so it makes the suite weaker than its headline count suggests. Set
  `allow_skips = true` when the skips are deliberate.
- The summary line reports counts from the run, not the spec.

## Limits

- **Linux and macOS.** Windows is untested: the tool relies on symlink
  semantics and POSIX path handling, and CI runs on Ubuntu only.
- **UTF-8 source only.** A target in another encoding is reported STALE in
  copy mode, and is a spec error in stage mode, rather than edited.
- **The sandbox is imported through `sys.path`.** The copy is put first on
  `PYTHONPATH`, is the suite's working directory, and any `PYTHONPATH` entry
  naming the project or a directory inside it is remapped into the copy. A
  project that is importable only through an
  installed distribution (a `src/` layout under `pip install -e .`, or a
  non-editable install into the venv named by `python`) keeps importing the
  installed code, and every mutant survives. Use a flat layout, or a
  `command` that installs the sandbox first.
- **Every run in copy mode copies the tree.** That is the isolation, and it is
  fine for a few dozen mutants over a repository of ordinary size. Trim the
  copy with `ignore` when it is not. Stage mode copies only the staged file.
- **A mutant that compiles but cannot be imported is a catch, not BROKEN, when
  the suite imports inside a test body.** The import error reaches the suite as
  a failing test, which is indistinguishable from the mutation being noticed.
  Importing the file ourselves to tell them apart would run its top-level code,
  which mutt_check has no business causing.
- **Only Python targets are compile-checked.** A data file a mutant makes
  unparseable is left to the suite.
- **One summary per run.** A command that runs unittest twice is judged by the
  last summary, so a skip or an import failure in the first run is not seen.
- **A target the operating system refuses to modify** (macOS `uchg`, or an
  immutable attribute) fails the run with exit 2 rather than being mutated.

## Checking mutt_check itself

```bash
make check
```

That runs ruff, then strict mypy against Python 3.9, then the unit suite, and
then mutt_check against its own suite using the
[`mutt_check.toml`](mutt_check.toml) in this repository, which reverts each of
the rules above one at a time. Both are offline and touch nothing outside a temp
directory. The unit suite takes seconds. The dogfood half runs one test class
per mutant, which for this repository's fifty mutants takes about a minute.

## License

MIT. See [LICENSE](LICENSE).
