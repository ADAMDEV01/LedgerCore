.PHONY: install dev migrate test lint typecheck up down

install:
	pip install -r requirements-dev.txt

migrate:
	alembic upgrade head

dev: migrate
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy app

up:
	docker compose up --build

down:
	docker compose down
