.PHONY: setup up down migrate seed run worker retention test lint format typecheck check

setup:  ## 依存関係をインストール
	uv sync

up:  ## PostgreSQL 16 を起動
	docker compose up -d --wait db

down:
	docker compose down

migrate:  ## マイグレーションを適用
	uv run alembic upgrade head

seed:  ## 初期データ（human actor / system actor / Fake AI社員2体）
	uv run python -m ai_business_explorer.seed

run:  ## API サーバー（Swagger: http://localhost:8000/docs）
	uv run uvicorn ai_business_explorer.main:app --reload

worker:  ## ステージ実行のワーカー（queued の実行を処理する）
	uv run python -m ai_business_explorer.worker

retention:  ## 保存期間を過ぎた LLM ログの本文を消す（cron から起動してもよい）
	uv run python -m ai_business_explorer.retention

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

check: lint typecheck test  ## CI と同じチェック
	uv run alembic check
