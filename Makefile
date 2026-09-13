# `make check` is the whole gate: the unit suite, then mutt_check run against
# itself. Both are offline and touch nothing outside a temp dir. The unit
# suite takes seconds; the dogfood half runs the suite again once per mutant,
# so it takes a minute or two. `make test` alone is the fast loop.

PYTHON ?= python3

.PHONY: help check test dogfood example clean

help:
	@echo "make check     unit suite, then mutt_check against its own suite"
	@echo "make test      unit suite only"
	@echo "make dogfood   mutt_check against its own suite only"
	@echo "make example   run the slugify example (one mutant survives on purpose)"

check: test dogfood

test:
	$(PYTHON) -B -m unittest discover -s tests -t .

dogfood:
	$(PYTHON) -B mutt_check.py mutt_check.toml

example:
	-cd examples/slugify && $(PYTHON) -B ../../mutt_check.py

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
