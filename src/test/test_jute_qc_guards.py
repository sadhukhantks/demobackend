"""Guards added 2026-09-03 (jute transfer chain review, Task C1).

- QC line UPDATE now also writes total_price.
- complete_inspection refuses to deactivate (reconcile away) a line that
  already has live (non-rejected) issues drawn against it.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def test_complete_inspection_blocks_deactivating_issued_line(mock_db):
    header = MagicMock()
    header._mapping = {"jute_mr_id": 1, "qc_check": 0, "branch_id": 29}
    mock_db.execute.return_value.fetchone.return_value = header
    mock_db.execute.return_value.fetchall.return_value = []
    # dropped-issues guard reads via .scalar() (COUNT(*))
    mock_db.execute.return_value.scalar.return_value = 1
    r = client.post(
        "/api/juteMaterialInspection/complete_inspection?co_id=2",
        json={"jute_mr_id": 1, "line_items": [], "lines_cleared": True, "save_only": False},
    )
    assert r.status_code == 409
    mock_db.commit.assert_not_called()
