# Repository Guidelines

## Project Structure & Module Organization
This repository is currently specification-first. Core design documents live in `docs/`, especially `docs/ARCHITECTURE.md`, `docs/PRD.md`, and the RFCs for the data pipeline and energy simulation. Source code is organized under `src/` by domain: `data_ingestion/`, `geometry/`, `simulation/`, `tile_generation/`, `visualization/`, and shared infrastructure in `shared/`. Use `tests/` for automated coverage, mirroring the `src/` layout when implementation begins.

## Build, Test, and Development Commands
There are no checked-in build files yet, so contributors should treat the docs as the source of truth for planned commands. Expected local entry points are:

- `uvicorn src.main:app --host 0.0.0.0 --port 8000` to run the FastAPI API.
- `celery -A src.shared.celery_app worker --loglevel=info` to start async workers.
- `docker compose up --build` to run the documented PostGIS, Redis, API, worker, and Nginx stack.

If you add a real `pyproject.toml`, `package.json`, or `Dockerfile`, update this guide in the same change.

## Coding Style & Naming Conventions
Use 4-space indentation for Python and standard TypeScript formatting if a frontend is added. Prefer `snake_case` for Python modules, functions, and Celery tasks; use `PascalCase` for React components; keep API paths noun-based, such as `/api/v1/buildings/{pnu}`. Match new modules to the existing domain folders instead of creating cross-cutting utility dumps.

## Testing Guidelines
Place tests in `tests/` with names like `test_data_ingestion.py` or `test_simulation_tasks.py`. Mirror package boundaries and cover PNU joins, geometry generation, tile output, and API response contracts. Add regression tests for every bug fix. When test tooling is introduced, document the exact command here.

## Commit & Pull Request Guidelines
This repo has no commit history yet, so start with short imperative commit subjects, for example `Add PostGIS connection settings`. Keep commits scoped to one concern. Pull requests should include a summary, affected paths, linked issue or task, and screenshots or sample payloads for UI or API changes.

## Security & Configuration Tips
Do not commit API keys, `.env` files, or downloaded public datasets. Keep secrets in environment variables and document required keys in the PR or docs when adding integrations.
