"""Tests for F024 Phase 3b rubric REST endpoints."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRubricEndpoints:
    @pytest.mark.asyncio
    async def test_get_rubric_returns_active(self):
        """Test GET /rubric with active rubric."""
        from nous.api.rest import create_app
        from nous.storage.models import RubricVersion

        mock_rv = MagicMock(spec=RubricVersion)
        mock_rv.id = uuid.uuid4()
        mock_rv.agent_id = "test"
        mock_rv.version = "1.0.0"
        mock_rv.parent_version = None
        mock_rv.change_reason = "Initial"
        mock_rv.dimensions = [
            {"name": "Recall", "weight": 0.25, "description": "test", "scoring_criteria": "test", "min_weight": 0.10, "max_weight": 0.40},
            {"name": "Tool Selection", "weight": 0.25, "description": "test", "scoring_criteria": "test", "min_weight": 0.10, "max_weight": 0.40},
            {"name": "Confidence Calibration", "weight": 0.25, "description": "test", "scoring_criteria": "test", "min_weight": 0.10, "max_weight": 0.40},
            {"name": "Proactivity", "weight": 0.25, "description": "test", "scoring_criteria": "test", "min_weight": 0.10, "max_weight": 0.40},
        ]
        mock_rv.outcome_correlations = {}
        mock_rv.status = "active"
        mock_rv.created_at = datetime.now(UTC)

        rubric_mgr = MagicMock()
        rubric_mgr.get_active = AsyncMock(return_value=mock_rv)
        from nous.cognitive.rubric_schemas import RubricVersionDetail, RubricDimension
        detail = RubricVersionDetail(
            id=mock_rv.id, agent_id="test", version="1.0.0",
            change_reason="Initial",
            dimensions=[RubricDimension(**d) for d in mock_rv.dimensions],
            status="active", created_at=mock_rv.created_at,
        )
        rubric_mgr.to_detail = MagicMock(return_value=detail)

        # Create app with rubric_manager
        from starlette.testclient import TestClient
        app = create_app(
            runner=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            cognitive=MagicMock(),
            database=MagicMock(),
            settings=MagicMock(agent_id="test"),
            rubric_manager=rubric_mgr,
        )
        client = TestClient(app)
        response = client.get("/rubric")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_get_rubric_returns_404_when_none(self):
        from nous.api.rest import create_app
        from starlette.testclient import TestClient

        rubric_mgr = MagicMock()
        rubric_mgr.get_active = AsyncMock(return_value=None)

        app = create_app(
            runner=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            cognitive=MagicMock(),
            database=MagicMock(),
            settings=MagicMock(agent_id="test"),
            rubric_manager=rubric_mgr,
        )
        client = TestClient(app)
        response = client.get("/rubric")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_rubric_history(self):
        from nous.api.rest import create_app
        from starlette.testclient import TestClient

        rubric_mgr = MagicMock()
        rubric_mgr.get_history = AsyncMock(return_value=[])

        app = create_app(
            runner=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            cognitive=MagicMock(),
            database=MagicMock(),
            settings=MagicMock(agent_id="test"),
            rubric_manager=rubric_mgr,
        )
        client = TestClient(app)
        response = client.get("/rubric/history")
        assert response.status_code == 200
        assert response.json() == []
