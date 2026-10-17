import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test-api.db"

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Outbox


# Keep the API tests isolated from a previous local run of the SQLite fixture.
Base.metadata.drop_all(bind=engine)


def test_login_join_state_and_idempotent_command():
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        session_id = login.json()["session_id"]

        join = client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "HOST"})
        assert join.status_code == 200

        payload = {
            "type": "CREATE_JOB",
            "idempotency_key": "test-create-job-1",
            "expected_entity_version": 0,
            "payload": {
                "kind": "B",
                "location": "B1",
                "pallets": 1,
                "weight_g": 10,
                "earliest_ms": 0,
                "latest_ms": 100000,
                "revenue_yen": 1000,
            },
        }
        first = client.post(f"/api/v1/sessions/{session_id}/commands", json=payload)
        second = client.post(f"/api/v1/sessions/{session_id}/commands", json=payload)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["command_id"] == second.json()["command_id"]

        state = client.get(f"/api/v1/sessions/{session_id}/state")
        assert state.status_code == 200
        assert state.json()["session"]["state_version"] >= 1


def test_api_auth_roles_conflicts_and_notifications():
    with TestClient(app) as client:
        assert client.get("/api/v1/sessions").status_code == 401
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401

        login = client.post("/api/v1/auth/login", json={"username": "shipper", "password": "shipper"})
        assert login.status_code == 200
        session_id = login.json()["session_id"]
        assert client.get("/api/v1/sessions").status_code == 200
        assert client.get("/api/v1/sessions/missing/state").status_code == 404
        assert client.post("/api/v1/sessions/missing/join", json={"role": "SHIPPER"}).status_code == 404

        assert client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "WAREHOUSE"}).status_code == 200
        forbidden = client.post(
            f"/api/v1/sessions/{session_id}/commands",
            json={"type": "CREATE_JOB", "idempotency_key": "forbidden-1", "payload": {}},
        )
        assert forbidden.status_code == 403
        assert client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "SHIPPER"}).status_code == 200
        assert client.post(f"/api/v1/sessions/{session_id}/plans", json={}).status_code == 403

        key = "conflict-" + uuid.uuid4().hex
        payload = {"kind": "B", "location": "Conflict", "pallets": 1, "weight_g": 1}
        accepted = client.post(
            f"/api/v1/sessions/{session_id}/commands",
            json={"type": "CREATE_JOB", "idempotency_key": key, "payload": payload},
        )
        assert accepted.status_code == 202
        conflict = client.post(
            f"/api/v1/sessions/{session_id}/commands",
            json={"type": "CREATE_JOB", "idempotency_key": key, "payload": {**payload, "location": "Other"}},
        )
        assert conflict.status_code == 409
        assert client.get("/api/v1/commands/not-found").status_code == 404
        assert client.get(f"/api/v1/sessions/{session_id}/plans/not-found").status_code == 404

        with SessionLocal() as db:
            db.add(Outbox(id=str(uuid.uuid4()), session_id=session_id, event_id=str(uuid.uuid4()), type="TEST_EVENT", payload={"event_seq": 1, "state_version": 1, "sim_time_ms": 0}))
            db.commit()
        state = client.get(f"/api/v1/sessions/{session_id}/state")
        assert state.status_code == 200
