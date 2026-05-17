# OpenLoadHub shared core

`backend/common` contains the shared configuration, models, schemas, clients, and utilities used by `ptp-admin`, `ptp-worker`, and `ptp-agent`.

The package is intentionally small in the public alpha. It keeps stable cross-service contracts in one place while service-specific behavior remains in each service package.

Directory layout:
```
backend/common/
├── config/            # Shared configuration and environment parsing
├── db/                # SQLAlchemy Base, session factory, and migration helpers
├── models/            # Shared ORM models such as Task, Script, Report, and User
├── schemas/           # Pydantic schemas reused across services
├── clients/           # Client wrappers for optional infrastructure integrations
├── logging/           # Structured logging helpers
└── utils/             # Shared utilities and constants
```

Usage:
- Install it as an editable package: `pip install -e backend/common`
- Or add `backend/common` to `PYTHONPATH`

Current shared modules:
- Configuration: `common/config/settings.py`; `ptp-admin` and `ptp-worker` keep compatibility exports through `app.core.config`.
- Enums: `common/models/enums.py` for task, script, approval, report, and related status/type values.
- Database: `common/db/database.py` for `Base`, `Session`, `engine`, and `get_db`.
- Schemas: shared task, script, approval, and report schemas in `common/schemas/*.py`.
- Agent requests: `ptp-agent` execution requests reuse the shared `EngineType`.
