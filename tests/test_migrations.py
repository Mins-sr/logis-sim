from __future__ import annotations

import importlib

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db import Base
from app.main import JoinRequest, join
from app.models import Membership, SimulationSession, User


migration = importlib.import_module("db.migrations.versions.0002_market_lifecycle")


def _legacy_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_membership_session_host")
        connection.execute(User.__table__.insert(), [
            {"id": "admin", "username": "admin", "password_hash": "x", "display_name": "Admin", "is_admin": True},
            {"id": "operator", "username": "operator", "password_hash": "x", "display_name": "Operator", "is_admin": False},
            {"id": "admin-2", "username": "admin-2", "password_hash": "x", "display_name": "Admin 2", "is_admin": True},
        ])
        connection.execute(Membership.__table__.insert(), [
            {"id": "host-operator", "session_id": "session-1", "user_id": "operator", "role": "HOST", "is_host": True, "is_warehouse_operator": True},
            {"id": "host-admin", "session_id": "session-1", "user_id": "admin", "role": "HOST", "is_host": True, "is_warehouse_operator": True},
            {"id": "host-admin-2", "session_id": "session-2", "user_id": "admin", "role": "HOST", "is_host": True, "is_warehouse_operator": True},
            {"id": "host-admin-3", "session_id": "session-2", "user_id": "admin-2", "role": "HOST", "is_host": True, "is_warehouse_operator": True},
        ])
    return engine


def test_0002_aborts_before_host_unique_index_when_legacy_hosts_are_invalid(monkeypatch):
    engine = _legacy_database()
    with engine.connect() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        with pytest.raises(migration.HostMembershipIntegrityError, match="non-administrator HOST.*multiple HOST"):
            migration.upgrade()
        assert "uq_membership_session_host" not in {item["name"] for item in inspect(connection).get_indexes("memberships")}


def test_0002_host_validation_accepts_one_administrator_host_per_session():
    engine = _legacy_database()
    with engine.begin() as connection:
        connection.execute(Membership.__table__.delete())
        connection.execute(Membership.__table__.insert(), {"id": "host-admin", "session_id": "session-1", "user_id": "admin", "role": "HOST", "is_host": True, "is_warehouse_operator": True})
        assert migration.validate_host_memberships(connection) is None


def test_admin_join_recovers_a_legacy_schema_after_migration_preflight_stops():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_membership_session_host")
        connection.exec_driver_sql("ALTER TABLE memberships DROP COLUMN next_notification_seq")
        connection.execute(User.__table__.insert(), [
            {"id": "legacy-admin", "username": "legacy-admin", "password_hash": "x", "display_name": "Admin", "is_admin": True},
            {"id": "legacy-operator", "username": "legacy-operator", "password_hash": "x", "display_name": "Operator", "is_admin": False},
        ])
        connection.execute(SimulationSession.__table__.insert(), {"id": "legacy-session", "name": "Legacy", "status": "RUNNING", "speed": 1.0, "state_version": 0, "event_seq": 0, "worker_generation": 0})
        memberships = Table("memberships", MetaData(), autoload_with=connection)
        connection.execute(memberships.insert(), {"id": "legacy-host", "session_id": "legacy-session", "user_id": "legacy-operator", "role": "HOST", "is_host": True, "is_warehouse_operator": True})

    with Session(engine) as db:
        admin = db.get(User, "legacy-admin")
        assert admin is not None
        result = join("legacy-session", JoinRequest(role="HOST"), user=admin, db=db)
        assert result["role"] == "HOST"
        memberships = Table("memberships", MetaData(), autoload_with=engine)
        rows = list(db.execute(select(memberships).where(memberships.c.session_id == "legacy-session")).mappings())
        assert {(row["user_id"], row["role"], row["is_host"]) for row in rows} == {
            ("legacy-admin", "HOST", True),
            ("legacy-operator", "SHIPPER", False),
        }
