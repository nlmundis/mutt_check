# `make check` is the whole gate: ruff, strict mypy against Python 3.9, the unit
# suite, then mutt_check run against itself. The tools are pinned in
# requirements-dev.txt: pip install -r requirements-dev.txt. All four are
# offline and touch nothing outside a temp dir. The unit
# suite takes seconds; the dogfood half runs the suite again once per mutant,
# so it takes a minute or two. `make test` alone is the fast loop.

PYTHON ?= python3
RUFF ?= ruff
MYPY ?= mypy

.PHONY: help check lint typecheck test dogfood example clean

help:
	@echo "make check     ruff, mypy, unit suite, then mutt_check against its own suite"
	@echo "make lint      ruff only"
	@echo "make typecheck mypy only, strict, against Python 3.9"
	@echo "make test      unit suite only"
	@echo "make dogfood   mutt_check against its own suite only"
	@echo "make example   run the slugify example (one mutant survives on purpose)"

check: lint typecheck test dogfood

lint:
	@command -v $(firstword $(RUFF)) >/dev/null 2>&1 || { \
	  echo "ruff not installed: pip install -r requirements-dev.txt"; exit 1; }
	$(RUFF) check .

typecheck:
	@command -v $(firstword $(MYPY)) >/dev/null 2>&1 || { \
	  echo "mypy not installed: pip install -r requirements-dev.txt"; exit 1; }
	$(MYPY)

test:
	$(PYTHON) -B -m unittest discover -s tests -t .

dogfood:
	$(PYTHON) -B mutt_check.py mutt_check.toml

example:
	-cd examples/slugify && $(PYTHON) -B ../../mutt_check.py

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
