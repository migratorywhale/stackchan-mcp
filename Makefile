.PHONY: lint lint-python lint-firmware test test-python test-mcp mcp-test test-firmware-cpp build-firmware

lint: lint-python lint-firmware

lint-python:
	uv run ruff check .

lint-firmware:
	cd firmware && pio check -e m5stack-cores3 --severity=high --fail-on-defect=high

test: test-python test-firmware-cpp build-firmware

test-python:
	uv run pytest

test-mcp:
	uv run pytest tests/test_mcp_server.py

mcp-test: test-mcp

test-firmware-cpp:
	cd firmware && pio test -e native

build-firmware:
	cd firmware && pio run -e m5stack-cores3
