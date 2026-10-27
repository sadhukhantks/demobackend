## Vowerp3be — AI coding agent instructions

These notes help an AI coding agent get productive quickly in this backend repo (FastAPI + SQLAlchemy, multi-tenant). Keep suggestions focused, minimal, and safe.

1) Big picture
- FastAPI app in `src/main.py` that mounts many routers. The item master APIs live under `/api/itemMaster` in `src/masters/items.py`.
- Multi-tenant DB approach: `src/config/db.py` derives the tenant DB name from request headers (subdomain, x-forwarded-host, referer or explicit `subdomain` header) and constructs a tenant MySQL URL. `get_tenant_db` yields a SQLAlchemy session bound to that tenant DB.
- Data access: mix of SQLAlchemy ORM models (e.g. `src/masters/models.py`) and raw SQL Text queries in `src/masters/query.py`. Queries use `sqlalchemy.text()` with named bind params (e.g. `:co_id`, `:item_id`).
- **IMPORTANT**: For table structure context, **prefer using the ORM models** in `src/models/` (e.g., `src/models/procurement.py` for procurement tables like `ProcInward`, `ProcInwardDtl`, `ProcPo`, etc.). These models reflect the **current database schema** and should be the authoritative source for column names and types. The SQL files under `dbqueries/` (e.g., `procurement.sql`, `usertables.sql`) may be **outdated** and should only be used as secondary reference for understanding table relationships or generating initial migrations.
- Key folders under `src/`:
  - `authorization/` — login flows, JWT refresh helpers, auth routers, and models used for validating users.
  - `common/` — shared logic split by persona (`companyAdmin`, `ctrldskAdmin`, `portal`); utilities here are reused across modules and tenants.
  - `config/` — project-wide configuration (database engines, CORS, environment loading). Treat this as the single source for connection/session helpers.
  - `masters/` — APIs and data access for master data. Follows the pattern of `models.py`, `query.py`, and feature routers (e.g. `items.py`).
  - Module folders (e.g. `procurement/`) — contain feature-specific endpoints. Each module should include `query.py` for SQL text, optional `models.py`/`schemas.py` for ORM/Pydantic definitions, and one or more router files (`indent.py`, etc.) that orchestrate requests. Modules may call into `masters` or `common` when business logic overlaps.
  - When introducing a new module, mirror the `procurement/` structure so DB mutations and schemas stay discoverable even if only part of the stack (e.g. `models.py`) is used initially.

2) How to run & test (developer workflow)
- **IMPORTANT**: Always activate the virtual environment first before running any Python commands:
  ```bash
  source .venv/Scripts/activate
  ```
- Local dev (venv): activate your Python environment and run the app with uvicorn: `uvicorn src.main:app --reload --host 0.0.0.0 --port 8000`.
- Docker: project includes a Dockerfile. Build: `docker build -t vowerp-backend .` and run with `docker run -d -p 8000:8000 --env-file .env --name vowerp3be vowerp3be-docker`.
- CI: workflow in `.github/workflows/deploy.yml` installs deps and builds/pushes a Docker image to ECR; tests may be run inside the built image using `pytest`.

3) Important repo conventions & patterns
- Multi-tenant lookup: Always pass `co_id` or rely on `get_tenant_db` which uses request headers to select the tenant DB. Many endpoints expect `co_id` as a query param; validate it early (raise 400 if missing).
- SQL Text queries: `src/masters/query.py` functions return `sqlalchemy.text` objects. Always execute them with a parameter dict matching the named binds. Example: `db.execute(get_item_table(co_id), {"co_id": int(co_id), "search": search_param})`.
- Passing NULL: use Python `None` (not string "null"). When a query binds `:item_id` and you want SQL NULL, pass `{"item_id": None}`.
- UOM and other lookups: Some query functions (e.g. `get_uom_list`) accept no params; do not pass `co_id` unless the function signature requires it.

4) Auth and token handling
- Auth helpers are in `src/authorization/utils.py`. Endpoints commonly use `Depends(get_current_user_with_refresh)` which will re-issue an access token using a refresh token stored in the default DB when access token is expired. The function reads the refresh token from `con_user_master` using a DB session created from `src/config/db.py`'s `default_engine`.
- Cookie name: `access_token`. Production cookie domain is `.vowerp.co.in` when `ENV=production`.

5) Files and hotspots to inspect when fixing bugs
- `src/masters/items.py` — primary place for item APIs, duplicate checks, and recent fixes. Look here for parameter handling and error responses.
- `src/masters/query.py` — canonical SQL queries. If you change binds, update callers to pass the same named params.
- `src/config/db.py` — tenant resolution logic. Mistakes here can cause the wrong DB/connection to be used.
- `src/authorization/utils.py` — JWT creation/refresh/verification; modify carefully.

6) Examples to copy/paste
- Executing a parameterized text query and converting rows:
  - q = get_item_table(co_id)
  - rows = db.execute(q, {"co_id": int(co_id), "search": search_param}).fetchall()
  - data = [dict(r._mapping) for r in rows]

7) Common pitfalls
- Do not call `int("null")`. Use `None` to represent SQL NULL when passing binds.
- Keep parameter names consistent: `:co_id`, `:item_id`, `:item_grp_id`, `:search` are used widely.
- Many functions return `text(sql)`; they are not ORM queries and must be executed via `db.execute()`.
- When adding or updating endpoints, run a quick smoke test with the tenant header or `co_id` query param to ensure `get_tenant_db` picks the right DB.

8) When to prefer ORM vs text SQL
- Use raw `text()` queries when they contain complex recursive CTEs (the project already uses them for hierarchical item groups). Use ORM for simple CRUD and when adding new models if consistent with surrounding code.

9) Safety & tests
- Make minimal changes to existing SQL unless necessary; modify callers first to match parameter names.
- Add small unit tests in `test/` when changing business logic; the repo uses `pytest`.

10) Testing requirements (MANDATORY for new/modified code)

When adding new endpoints, functions, or modifying existing code, **always write tests** to validate the changes. This ensures a clear picture of what works and what doesn't.

### Test file location & naming
- Place tests in `src/test/` directory
- Name test files: `test_{module}_{feature}.py` (e.g., `test_procurement_indent.py`, `test_masters_items.py`)
- Use pytest conventions: test functions start with `test_`

### What to test
| Code Change | Required Tests |
|-------------|----------------|
| New endpoint | Request/response validation, error cases, auth requirements |
| New query function | Parameter binding, expected SQL output, edge cases |
| Business logic | Input/output validation, boundary conditions, error handling |
| Bug fix | Regression test that would have caught the bug |

### Test patterns to follow

**Endpoint tests (using FastAPI TestClient):**
```python
# src/test/test_procurement_po.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from src.main import app

client = TestClient(app)

class TestPOEndpoints:
    """Tests for Purchase Order API endpoints."""

    def test_get_po_list_requires_co_id(self):
        """Should return 400 when co_id is missing."""
        response = client.get("/api/procurement/po/list")
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    def test_get_po_list_success(self):
        """Should return PO list for valid co_id."""
        response = client.get("/api/procurement/po/list?co_id=1")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    @patch("src.procurement.po.get_tenant_db")
    def test_get_po_by_id_not_found(self, mock_db):
        """Should return 404 when PO doesn't exist."""
        mock_session = MagicMock()
        mock_session.execute.return_value.fetchone.return_value = None
        mock_db.return_value.__enter__.return_value = mock_session

        response = client.get("/api/procurement/po/999?co_id=1")
        assert response.status_code == 404
```

**Query function tests:**
```python
# src/test/test_masters_query.py
import pytest
from sqlalchemy import text
from src.masters.query import get_item_table, get_uom_list

class TestMasterQueries:
    """Tests for master data SQL query functions."""

    def test_get_item_table_returns_text_object(self):
        """Query function should return sqlalchemy text object."""
        result = get_item_table(1)
        assert isinstance(result, type(text("")))

    def test_get_item_table_contains_required_binds(self):
        """Query should have :co_id and :search bind parameters."""
        result = get_item_table(1)
        sql_str = str(result)
        assert ":co_id" in sql_str
        assert ":search" in sql_str

    def test_get_uom_list_no_params_required(self):
        """UOM list query should work without parameters."""
        result = get_uom_list()
        assert result is not None
```

**Business logic tests:**
```python
# src/test/test_procurement_calculations.py
import pytest
from src.procurement.utils import calculate_line_total, calculate_tax

class TestProcurementCalculations:
    """Tests for procurement business logic."""

    @pytest.mark.parametrize("qty,rate,expected", [
        (10, 100.0, 1000.0),
        (0, 100.0, 0.0),
        (5, 0.0, 0.0),
        (2.5, 40.0, 100.0),
    ])
    def test_calculate_line_total(self, qty, rate, expected):
        """Line total should be quantity × rate."""
        assert calculate_line_total(qty, rate) == expected

    def test_calculate_tax_gst_18(self):
        """Should calculate 18% GST correctly."""
        result = calculate_tax(1000.0, tax_rate=18.0)
        assert result == 180.0

    def test_calculate_tax_zero_amount(self):
        """Zero amount should return zero tax."""
        assert calculate_tax(0.0, tax_rate=18.0) == 0.0
```

**Mocking database for isolation:**
```python
# src/test/test_items_create.py
import pytest
from unittest.mock import patch, MagicMock
from src.masters.items import check_duplicate_item

class TestItemDuplicateCheck:
    """Tests for item duplicate validation."""

    @patch("src.masters.items.get_tenant_db")
    def test_duplicate_found_returns_true(self, mock_db):
        """Should return True when duplicate item exists."""
        mock_session = MagicMock()
        mock_session.execute.return_value.fetchone.return_value = {"count": 1}
        mock_db.return_value.__enter__.return_value = mock_session

        result = check_duplicate_item(co_id=1, item_code="ITEM001")
        assert result is True

    @patch("src.masters.items.get_tenant_db")
    def test_no_duplicate_returns_false(self, mock_db):
        """Should return False when no duplicate exists."""
        mock_session = MagicMock()
        mock_session.execute.return_value.fetchone.return_value = {"count": 0}
        mock_db.return_value.__enter__.return_value = mock_session

        result = check_duplicate_item(co_id=1, item_code="NEWITEM")
        assert result is False
```

### Running tests
```bash
# FIRST: Activate the virtual environment (required before any Python commands)
source .venv/Scripts/activate

# Run all tests
pytest src/test/ -v

# Run specific test file
pytest src/test/test_procurement_po.py -v

# Run tests matching a pattern
pytest src/test/ -k "test_po" -v

# Run with coverage
pytest src/test/ --cov=src --cov-report=html
```

### Test checklist for PRs
Before submitting code changes, ensure:
- [ ] All new functions have corresponding tests
- [ ] All modified functions have updated tests (if behavior changed)
- [ ] Edge cases are covered (empty inputs, None values, boundary conditions)
- [ ] Error paths are tested (what happens when things fail?)
- [ ] Tests pass locally: `pytest src/test/ -v`
- [ ] No hardcoded tenant/company IDs that would fail in other environments

If any section is unclear or you'd like examples for a concrete endpoint (for example, `CreateItem` or `item_create_setup`) tell me which file or flow and I'll expand the instructions with precise code snippets and tests.
