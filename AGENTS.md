# Working on mutt_check

mutt_check proves a test suite catches the defects it claims to pin. It applies
the curated mutants listed in `mutt_check.toml` to a throwaway copy of a
project, runs the suite, and requires the suite to go red for every one.

One module, `mutt_check.py`. Python 3.9 or newer, Linux or macOS. Standard
library only, except `tomli` on 3.10 and earlier, where the stdlib has no
`tomllib`. Nothing here needs network access.

## Commands

```bash
make check     # the gate: ruff, mypy, unit suite, then mutt_check against its own suite
make lint      # ruff only
make typecheck # mypy only, strict, against Python 3.9
make test      # unit suite only, a few seconds
make dogfood   # the self-mutation run only, about a minute
make example   # the worked example, where one mutant survives on purpose
```

`make check` has to be green before any change here is done. It writes nothing
outside a temporary directory, so it is safe to run at any time. Run the unit
suite under each supported interpreter when you touch anything version
sensitive:

```bash
python3.9 -B -m unittest discover -s tests -t .
```

## Reading a run

Judge by the exit code first: 0 means every mutant was caught, 1 means at least
one survived, went stale or broke, and 2 means the run could not judge anything.
Both 1 and 2 are failures.

- `SURVIVED` means no test noticed that decision being reverted. Write the test
  that would, rather than deleting the mutant.
- `STALE` means the mutant's anchor text moved, so that mutant tested nothing.
  Realign the anchor in the same commit as the change that moved it.
- `BROKEN` means the suite could not deliver a verdict at all, usually because
  the mutated file no longer loads. Rewrite the mutant so the code still loads.

## Rules for changing this repository

- A mutant goes into `mutt_check.toml` only together with the test that kills
  it, and each one names that test class in `suites`. A known survivor in the
  dogfood spec is a to-do dressed as evidence.
- When you change a line a mutant anchors on, realign that mutant in the same
  commit. The gate will call it STALE, so you will know.
- No test may skip. A skipped test makes the dogfood control red, because a
  skipped test can catch nothing.
- No new dependencies. The only one is `tomli`, and only where the standard
  library has no `tomllib`. Anything else is answered by the stdlib.
- New code must run on the floor version, 3.9: no `match`, no runtime `X | Y`
  unions, no `removeprefix`, no `Path.is_relative_to`. Annotations are fine,
  since the module imports `annotations` from `__future__`.
- Never weaken a guard to make a test pass. Every guard here exists because a
  wrong result once read as success.
- A name and its docstring must let a reader predict what a function does, and
  why a caller would reach for it, without opening the body.
- Anchors in the spec must appear exactly once in their file. Include a
  neighbouring line when a line alone is ambiguous.

## Using mutt_check on another project

Copy `mutt_check.py` into the project or `pip install mutt_check`, write a
`mutt_check.toml` naming the decisions the suite is supposed to pin, and run
`mutt_check` in CI. The README has the spec format, the verdict table and a
worked example.
