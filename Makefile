.PHONY: help install lint format typecheck test check up down ingest serve clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the package with dev dependencies
	pip install -e ".[dev]"

lint: ## Run ruff lint checks
	ruff check src tests

format: ## Auto-format the codebase
	ruff format src tests

typecheck: ## Run mypy (strict)
	mypy

test: ## Run the test suite (no keys / DB required)
	pytest --cov=ragchat --cov-report=term-missing

check: lint typecheck test ## Run all quality gates

up: ## Start Postgres+pgvector and the API via docker compose
	docker compose up --build

down: ## Stop and remove the compose stack
	docker compose down

ingest: ## Ingest content.md into the knowledge base (needs a running DB + keys)
	ragchat ingest content.md

serve: ## Run the API locally with reload (needs a running DB + keys)
	ragchat serve --reload

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
