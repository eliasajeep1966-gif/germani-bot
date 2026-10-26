# SECURITY CHANGELOG

## 2026-10-26 — Dockerized Deployment and Persistent Database Storage

- **Files Modified:**
  - `Dockerfile` — added a Python 3.12 slim runtime, pinned dependency installation, non-root `botuser`, and unbuffered execution.
  - `docker-compose.yml` — added restart policy, graceful stop period, external `.env` loading, and a named persistent volume for SQLite state.
  - `.dockerignore` — excludes `.env`, databases, logs, caches, and Git metadata from the build context.
  - `.env.example` — documents required and optional runtime variables without real secrets.
  - `config.py` — added the `DB_PATH` environment override while preserving the local default path.
- **Fix/Deployment Description:**
  - The bot can run reproducibly on a server without embedding secrets in the image.
  - SQLite data is stored in the `germani_bot_runtime` Docker volume and survives container recreation or image rebuilds.
  - The container runs as an unprivileged user; only the mounted runtime directory is writable.
- **Verification Required:**
  - Build the image and run `python -B -m py_compile` for all Python files.
  - Start with a real server-side `.env`; never commit `.env`.
