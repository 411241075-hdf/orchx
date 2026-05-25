# orchX development Makefile.

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYBIN := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy

.PHONY: help install install-all test test-unit test-integration test-cov \
        lint fmt typecheck check clean build worker-image _fix-macos-hidden-pth

help:
	@echo "orchX dev tasks:"
	@echo "  make install         — venv + editable + dev/test extras (uses compat editable_mode for Python 3.14)"
	@echo "  make install-all     — everything including server/docker/mcp/browser"
	@echo "  make test            — pytest"
	@echo "  make test-unit       — unit-tests only"
	@echo "  make test-integration— integration tests only"
	@echo "  make test-cov        — pytest + coverage report"
	@echo "  make lint            — ruff check"
	@echo "  make fmt             — ruff format"
	@echo "  make typecheck       — mypy"
	@echo "  make check           — lint + typecheck + test"
	@echo "  make build           — sdist + wheel"
	@echo "  make worker-image    — docker build orchx-worker:latest"

$(VENV):
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)
	$(PIP) install --config-settings editable_mode=compat -e ".[dev,test]"
	@$(MAKE) --no-print-directory _fix-macos-hidden-pth

install-all: $(VENV)
	$(PIP) install --config-settings editable_mode=compat -e ".[dev,test,server,mcp,docker,browser,memory-embed,pydantic]"
	@$(MAKE) --no-print-directory _fix-macos-hidden-pth

# macOS Sequoia + Python 3.14: pip-созданные .pth файлы получают
# флаг UF_HIDDEN, и site.py их пропускает (см. site.py:185). Снимаем
# флаг сразу после install — без этого `import orchx` падает.
.PHONY: _fix-macos-hidden-pth
_fix-macos-hidden-pth:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		chflags nohidden $(VENV)/lib/python*/site-packages/*.pth 2>/dev/null || true; \
	fi

test: $(VENV)
	$(PYTEST)

test-unit: $(VENV)
	$(PYTEST) src/orchx/tests/unit -v

test-integration: $(VENV)
	$(PYTEST) src/orchx/tests/integration -v

test-cov: $(VENV)
	$(PYTEST) --cov=orchx --cov-report=term --cov-report=html

lint: $(VENV)
	$(RUFF) check src/

fmt: $(VENV)
	$(RUFF) format src/

typecheck: $(VENV)
	$(MYPY) src/orchx

check: lint typecheck test

build: $(VENV)
	$(PIP) install build
	$(PYBIN) -m build

worker-image:
	docker build -f src/orchx/templates/runtime/Dockerfile.worker -t orchx-worker:latest .

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
