PYTHON ?= python3

.PHONY: test run demo

test:
	$(PYTHON) -m unittest discover -v

run:
	$(PYTHON) djamp.py

demo:
	$(PYTHON) djamp.py --demo
