PYTHON ?= python3

.PHONY: test run demo backend

test:
	$(PYTHON) -m unittest discover -v

backend:
	./scripts/build-backend

run:
	$(PYTHON) djamp.py

demo:
	$(PYTHON) djamp.py --demo
