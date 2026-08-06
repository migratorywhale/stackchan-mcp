.PHONY: install-hooks lint lint-python typecheck-python lint-firmware test test-python test-mcp mcp-test test-firmware-cpp build-firmware audit-security audit-dependencies ci-local

install-hooks:
	git config core.hooksPath .githooks

lint: lint-python typecheck-python lint-firmware

lint-python:
	uv run ruff check .

typecheck-python:
	uv run pyright

lint-firmware:
	cd firmware && pio check -e m5stack-cores3 --severity=high --fail-on-defect=high

test: test-python test-firmware-cpp build-firmware

test-python:
	uv run pytest --cov=mcp_server --cov=scripts --cov-report=term-missing

test-mcp:
	uv run pytest tests/test_mcp_server.py

mcp-test: test-mcp

test-firmware-cpp:
	cd firmware && pio test -e native

build-firmware:
	cd firmware && pio run -e m5stack-cores3

audit-security:
	uv run python scripts/ci_security_audit.py

audit-dependencies:
	tmp_requirements="$$(mktemp)"; \
	trap 'rm -f "$$tmp_requirements"' EXIT; \
	uv export --locked --no-dev --no-hashes -o "$$tmp_requirements"; \
	uv run --locked pip-audit -r "$$tmp_requirements"

ci-local: lint test audit-security audit-dependencies
