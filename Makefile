.PHONY: lint test run-debug

lint:
	ruff check .
	ruff format --check .

test:
	pytest -q

run-debug:
	python analyze.py --debug
