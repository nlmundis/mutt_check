# `make check` is the whole gate: the unit suite, then mutcheck run against
# itself. Both are offline, touch nothing outside a temp dir, and finish in
# seconds, so there is no reason to claim "done" without running it.

PYTHON ?= python3

.PHONY: help check test dogfood example clean

help:
	@echo "make check     unit suite, then mutcheck against its own suite"
	@echo "make test      unit suite only"
	@echo "make dogfood   mutcheck against its own suite only"
	@echo "make example   run the slugify example (one mutant survives on purpose)"

check: test dogfood

test:
	$(PYTHON) -B -m unittest discover -s tests -t .

dogfood:
	$(PYTHON) -B mutcheck.py mutcheck.toml

example:
	-cd examples/slugify && $(PYTHON) -B ../../mutcheck.py

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
