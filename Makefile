.PHONY: up down migrate test lint inspect match-report read-pdfs compare-cruce evaluar-hibrido checks ui

up:
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose exec api alembic upgrade head

test:
	docker compose run --rm --no-deps api pytest

lint:
	docker compose run --rm --no-deps api sh -c "ruff check . && ruff format --check ."
	cd frontend && npm run lint

inspect:
	docker compose run --rm --no-deps api python scripts/inspect_ingest.py

match-report:
	docker compose run --rm api python scripts/match_report.py

read-pdfs:
	docker compose run --rm api python -m app.lectura /data/DocJustificativa/22-PY-136\ -\ AECID --convencion dot_thousands --moneda PYG

compare-cruce:
	docker compose run --rm api python scripts/comparar_cruce.py

evaluar-hibrido:
	docker compose run --rm api python scripts/evaluar_hibrido.py

checks:
	docker compose run --rm api python scripts/checks_report.py

ui:
	docker compose up --build ui
