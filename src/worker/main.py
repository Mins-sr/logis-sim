from __future__ import annotations

import json
import os
import time
import uuid
from datetime import timedelta, timezone
from typing import Any

import httpx
import psycopg
from sqlalchemy import select

from app.db import SessionLocal, wait_for_database
from app.models import Command, DomainEvent, Job, Membership, Outbox, Plan, SimulationSession, Snapshot, Truck, utcnow
from shared.warehouse import WarehouseAdapter, WarehouseInvariantError


WORKER_ID = os.getenv("WORKER_ID", f"worker-{uuid.uuid4().hex[:12]}")
OPTIMIZER_URL = os.getenv("OPTIMIZER_URL", "http://optimizer:8001")
LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "10"))


def new_id() -> str:
    return str(uuid.uuid4())


def claim_sessions() -> dict[str, int]:
    claimed: dict[str, int] = {}
    with SessionLocal() as db:
        rows = list(db.scalars(select(SimulationSession).where(SimulationSession.status.in_(["RUNNING", "PAUSED"]))))
        for session in rows:
            lease_until = session.lease_until.replace(tzinfo=timezone.utc) if session.lease_until and session.lease_until.tzinfo is None else session.lease_until
            expired = lease_until is None or lease_until < utcnow()
            if session.owner_id not in {None, WORKER_ID} and not expired:
                continue
            session.owner_id = WORKER_ID
            session.worker_generation += 1
            session.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
            claimed[session.id] = session.worker_generation
        db.commit()
    return claimed


def append_event(db, session: SimulationSession, event_type: str, payload: dict[str, Any], *, increment_version: bool = True) -> None:
    if increment_version:
        session.state_version += 1
    session.event_seq += 1
    event_payload = {**payload, "event_seq": session.event_seq, "state_version": session.state_version, "sim_time_ms": session.current_sim_time_ms}
    event = DomainEvent(id=new_id(), session_id=session.id, event_seq=session.event_seq, sim_time_ms=session.current_sim_time_ms, state_version=session.state_version, type=event_type, payload=event_payload)
    db.add(event)
    db.add(Outbox(id=new_id(), session_id=session.id, event_id=event.id, type=event_type, payload=event_payload))


def reject(command: Command, reason: str, result: dict[str, Any] | None = None) -> None:
    command.status = "REJECTED"
    command.rejection_reason = reason
    command.result = result or {"reason": reason}
    command.processed_at = utcnow()


def apply_command(db, session: SimulationSession, command: Command) -> None:
    payload = command.payload or {}
    membership = db.scalar(select(Membership).where(Membership.session_id == session.id, Membership.user_id == command.user_id))
    if membership is None:
        reject(command, "MEMBERSHIP_REQUIRED")
        return
    if command.type == "CREATE_JOB":
        if membership.role not in {"SHIPPER", "HOST"}:
            reject(command, "FORBIDDEN")
            return
        kind = payload.get("kind")
        if kind not in {"L", "B"}:
            reject(command, "INVALID_JOB_KIND")
            return
        job_id = str(payload.get("id") or new_id())
        if db.get(Job, job_id):
            reject(command, "DUPLICATE_JOB")
            return
        job = Job(
            id=job_id,
            session_id=session.id,
            kind=kind,
            location=str(payload.get("location", "")),
            pallets=int(payload.get("pallets", 0)),
            weight_g=int(payload.get("weight_g", 0)),
            earliest_ms=int(payload.get("earliest_ms", 0)),
            latest_ms=int(payload.get("latest_ms", 86_400_000)),
            service_ms=int(payload.get("service_ms", 300_000)),
            revenue_yen=int(payload.get("revenue_yen", 0)),
        )
        if not job.location or job.pallets <= 0 or job.weight_g < 0 or job.earliest_ms > job.latest_ms:
            reject(command, "INVALID_JOB")
            return
        db.add(job)
        command.status = "APPLIED"
        command.result = {"job_id": job.id}
        append_event(db, session, "JOB_CREATED", {"job_id": job.id, "kind": job.kind, "location": job.location})
    elif command.type == "ACCEPT_JOB":
        job = db.get(Job, str(payload.get("job_id")))
        truck = db.get(Truck, str(payload.get("truck_id")))
        if job is None or truck is None or job.session_id != session.id or truck.session_id != session.id:
            reject(command, "NOT_FOUND")
            return
        if job.version != command.expected_entity_version:
            reject(command, "VERSION_CONFLICT", {"current_version": job.version})
            return
        if job.status != "OPEN":
            reject(command, "JOB_NOT_OPEN")
            return
        if membership.role != "HOST" and truck.assigned_user_id not in {None, command.user_id}:
            reject(command, "TRUCK_NOT_ASSIGNED")
            return
        linehaul_pallets = sum(row.pallets for row in db.scalars(select(Job).where(Job.session_id == session.id, Job.kind == "L", Job.status.in_(["OPEN", "CONTRACTED", "IN_PROGRESS"]))))
        linehaul_weight = sum(row.weight_g for row in db.scalars(select(Job).where(Job.session_id == session.id, Job.kind == "L", Job.status.in_(["OPEN", "CONTRACTED", "IN_PROGRESS"]))))
        if job.kind == "L" and (linehaul_pallets > truck.capacity_pallets or linehaul_weight > truck.capacity_weight_g):
            reject(command, "CAPACITY_EXCEEDED")
            return
        job.status = "CONTRACTED"
        job.contracted_truck_id = truck.id
        job.version += 1
        truck.status = "LOADING"
        truck.version += 1
        command.status = "APPLIED"
        command.result = {"job_id": job.id, "truck_id": truck.id}
        append_event(db, session, "JOB_CONTRACTED", {"job_id": job.id, "truck_id": truck.id})
    elif command.type in {"PAUSE", "RESUME", "SET_SPEED"}:
        if not membership.is_host:
            reject(command, "FORBIDDEN")
            return
        if command.type == "PAUSE":
            session.status = "PAUSED"
        elif command.type == "RESUME":
            session.status = "RUNNING"
        else:
            speed = float(payload.get("speed", 1))
            if speed not in {0, 1, 10, 60}:
                reject(command, "INVALID_SPEED")
                return
            session.speed = speed
        command.status = "APPLIED"
        command.result = {"session_status": session.status, "speed": session.speed}
        append_event(db, session, "SESSION_CHANGED", {"status": session.status, "speed": session.speed})
    elif command.type == "SAVE_SNAPSHOT":
        warehouse = WarehouseAdapter(agv_ids=["agv-1", "agv-2", "agv-3"], staging_capacity=40, sim_time_ms=session.current_sim_time_ms, event_seq=session.event_seq)
        snapshot = warehouse.export_snapshot()
        db.add(Snapshot(id=new_id(), session_id=session.id, snapshot_format_version=1, engine_build_id=snapshot["engine_build_id"], last_applied_event_seq=session.event_seq, state=snapshot))
        command.status = "APPLIED"
        command.result = {"event_seq": session.event_seq}
        append_event(db, session, "SNAPSHOT_SAVED", {"last_applied_event_seq": session.event_seq})
    else:
        reject(command, "UNKNOWN_COMMAND")
        return
    command.processed_at = utcnow()


def process_commands(session_id: str, generation: int) -> None:
    with SessionLocal() as db:
        session = db.get(SimulationSession, session_id)
        if session is None or session.owner_id != WORKER_ID or session.worker_generation != generation:
            return
        session.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
        command = db.scalar(select(Command).where(Command.session_id == session_id, Command.status == "ACCEPTED").order_by(Command.created_at).with_for_update())
        if command is not None:
            apply_command(db, session, command)
        db.commit()


def advance_clock(session_id: str, generation: int) -> None:
    with SessionLocal() as db:
        session = db.get(SimulationSession, session_id)
        if session is None or session.owner_id != WORKER_ID or session.worker_generation != generation or session.status != "RUNNING":
            return
        session.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
        session.current_sim_time_ms += int(200 * max(0, session.speed))
        db.commit()


def dispatch_plans() -> None:
    with SessionLocal() as db:
        plans = list(db.scalars(select(Plan).where(Plan.status == "PROPOSED").order_by(Plan.requested_at).limit(10)))
        for plan in plans:
            plan.status = "VALIDATING"
        db.commit()
    for plan in plans:
        try:
            with httpx.Client(timeout=1.0) as client:
                response = client.post(f"{OPTIMIZER_URL}/optimization-jobs", json={"plan_id": plan.id})
            if response.status_code >= 300:
                raise RuntimeError(f"optimizer returned {response.status_code}")
        except Exception:
            with SessionLocal() as db:
                stale = db.get(Plan, plan.id)
                if stale and stale.status == "VALIDATING":
                    stale.status = "PROPOSED"
                    db.commit()


def open_command_listener():
    """Register LISTEN after connect; polling remains the lossless fallback."""
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        return None
    connection = psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True)
    connection.execute("LISTEN commands")
    return connection


def main() -> None:
    wait_for_database()
    last_claim = 0.0
    claimed: dict[str, int] = {}
    listener = open_command_listener()
    while True:
        if listener is not None:
            try:
                # NOTIFY is only a wake-up hint. The query below is the authoritative
                # source, so a reconnect cannot lose a command.
                next(listener.notifies(timeout=0.0), None)
            except Exception:
                listener.close()
                listener = open_command_listener()
        if time.monotonic() - last_claim > max(1, LEASE_SECONDS // 2):
            claimed.update(claim_sessions())
            last_claim = time.monotonic()
        for session_id, generation in list(claimed.items()):
            process_commands(session_id, generation)
            advance_clock(session_id, generation)
        dispatch_plans()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
