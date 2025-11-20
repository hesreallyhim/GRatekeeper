# Default Python interpreter can be overridden: `make test PYTHON=$(which python3)`
PYTHON ?=
VENV ?= venv
STAMP_DIR ?= .stamps
DEV_STAMP := $(STAMP_DIR)/dev.stamp

.PHONY: all setup deps test lint coverage build wheel sdist install probe clean

all: test

$(STAMP_DIR):
	@mkdir -p $(STAMP_DIR)

$(VENV):
	@python3 -m venv $(VENV)

$(DEV_STAMP): pyproject.toml | $(STAMP_DIR) $(VENV)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	PIP_CMD="$$PYTHON -m pip"; \
	$$PIP_CMD install --upgrade pip; \
	$$PIP_CMD install -e .[dev]; \
	touch $(DEV_STAMP)

setup: $(DEV_STAMP)
deps: setup

test: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m pytest

lint: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m compileall src tests

coverage: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m pytest --cov=src/gratekeeper --cov-report=term-missing

build: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m pip install --upgrade build; \
	$$PYTHON -m build

wheel: build

sdist: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m pip install --upgrade build; \
	$$PYTHON -m build --sdist

install: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON -m pip install --force-reinstall .

probe: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	$$PYTHON scripts/rate_limit_probe.py --help

demo: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	GRATEKEEPER_RUN_DEMOS=1 GITHUB_TOKEN=$$GITHUB_TOKEN $$PYTHON scripts/run_demo_scenarios.py

demo-test: $(DEV_STAMP)
	@PYTHON=$${PYTHON:-$(VENV)/bin/python3}; \
	GRATEKEEPER_RUN_DEMOS=1 GITHUB_TOKEN=$$GITHUB_TOKEN $$PYTHON -m pytest -m demo

demo-ui:
	@./scripts/run_dashboard_demo.sh

demo-ui-gif: demo-ui
	vhs render demo.cast --output demo.gif

clean:
	@rm -rf $(STAMP_DIR) dist *.egg-info .pytest_cache
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
