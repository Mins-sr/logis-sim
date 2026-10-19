from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import timedelta, timezone
from typing import Any

import httpx
import psycopg
from sqlalchemy import func, select

from app.db import SessionLocal, wait_for_database
from app.models import (
    Command,
    Dock,
    DomainEvent,
    Job,
    Membership,
    Outbox,
    Plan,
    ResourceReservation,
    SimulationSession,
    Snapshot,
    StagingArea,
    StreamNotification,
    Truck,
    Trip,
    User,
    utcnow,
)
from shared.warehouse import WarehouseAdapter


WORKER_ID = os.getenv("WORKER_ID", f"worker-{uuid.uuid4().hex[:12]}")
OPTIMIZER_URL = os.getenv("OPTIMIZER_URL", "http://optimizer:8001")
LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "10"))
AGV_IDS = ("agv-1", "agv-2", "agv-3")
TRAVEL_DURATION_MS = 1_000
LOAD_DURATION_MS = 1_000
UNLOAD_DURATION_MS = 1_000


class InvalidCommand(ValueError):
    """A queue item that can be rejected without stopping the worker."""


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidCommand(f"{key} must be a non-empty string")
    return value.strip()


def _integer(payload: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCommand(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise InvalidCommand(f"{key} must be >= {minimum}")
    return value


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidCommand(f"{key} must be finite")
    return float(value)


def new_id() -> str:
    return str(uuid.uuid4())


def claim_sessions() -> dict[str, int]:
    claimed: dict[str, int] = {}
    with SessionLocal() as db:
        rows = list(db.scalars(select(SimulationSession).where(SimulationSession.status.in_(["RUNNING", "PAUSED"])).with_for_update()))
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


def process_outbox(db, session_id: str | None = None) -> None:
    """Fan out committed events in event order; API reads never create notifications."""
    query = select(Outbox).where(Outbox.processed_at.is_(None))
    if session_id is not None:
        query = query.where(Outbox.session_id == session_id)
    rows = list(db.scalars(query))
    rows.sort(key=lambda row: (int((row.payload or {}).get("event_seq", 0)), row.id))
    for row in rows:
        memberships = list(db.scalars(select(Membership).where(Membership.session_id == row.session_id).order_by(Membership.user_id)))
        for original_membership in memberships:
            membership = db.scalar(select(Membership).where(Membership.id == original_membership.id).with_for_update())
            if membership is None:
                continue
            existing = db.scalar(
                select(StreamNotification).where(
                    StreamNotification.session_id == row.session_id,
                    StreamNotification.user_id == membership.user_id,
                    StreamNotification.event_id == row.event_id,
                )
            )
            if existing is not None:
                membership.next_notification_seq = max(membership.next_notification_seq, existing.seq)
                continue
            last_seq = db.scalar(
                select(func.max(StreamNotification.seq)).where(
                    StreamNotification.session_id == row.session_id,
                    StreamNotification.user_id == membership.user_id,
                )
            ) or 0
            next_seq = max(int(last_seq), membership.next_notification_seq) + 1
            membership.next_notification_seq = next_seq
            db.add(
                StreamNotification(
                    id=new_id(),
                    session_id=row.session_id,
                    user_id=membership.user_id,
                    event_id=row.event_id,
                    seq=next_seq,
                    event_seq=int((row.payload or {}).get("event_seq", 0)),
                    state_version=int((row.payload or {}).get("state_version", 0)),
                    sim_time_ms=int((row.payload or {}).get("sim_time_ms", 0)),
                    type=row.type,
                    payload=row.payload,
                )
            )
        row.processed_at = utcnow()
    if rows:
        db.commit()


def reject(command: Command, reason: str, result: dict[str, Any] | None = None) -> None:
    command.status = "REJECTED"
    command.rejection_reason = reason
    command.result = result or {"reason": reason}
    command.processed_at = utcnow()


def _reserve_trip_resources(db, session: SimulationSession, trip: Trip, *, staging_pallets: int = 0) -> bool:
    # SessionLocal disables autoflush. Flush reservation releases/additions
    # from an earlier trip in this same tick before calculating availability;
    # otherwise two trips can select the same dock or AGV and fail at commit.
    db.flush()
    active = list(db.scalars(select(ResourceReservation).where(ResourceReservation.session_id == session.id, ResourceReservation.released_at.is_(None))))
    reserved = {(row.resource_type, row.resource_id) for row in active}
    dock = next((row for row in db.scalars(select(Dock).where(Dock.session_id == session.id).order_by(Dock.id)) if row.occupied_by in {None, trip.id} and ("DOCK", row.id) not in reserved), None)
    agv = next((agv_id for agv_id in AGV_IDS if ("AGV", agv_id) not in reserved), None)
    staging = next((row for row in db.scalars(select(StagingArea).where(StagingArea.session_id == session.id).order_by(StagingArea.id)) if row.used_pallets + staging_pallets <= row.capacity_pallets and ("STAGING", row.id) not in reserved), None)
    if dock is None or agv is None or (staging_pallets > 0 and staging is None):
        return False
    dock.occupied_by = trip.id
    dock.version += 1
    db.add(ResourceReservation(id=new_id(), session_id=session.id, trip_id=trip.id, resource_type="DOCK", resource_id=dock.id))
    db.add(ResourceReservation(id=new_id(), session_id=session.id, trip_id=trip.id, resource_type="AGV", resource_id=agv))
    trip.reserved_dock_id = dock.id
    trip.reserved_agv_id = agv
    if staging is not None and staging_pallets > 0:
        staging.used_pallets += staging_pallets
        staging.version += 1
        trip.reserved_staging_id = staging.id
        trip.reserved_staging_pallets = staging_pallets
        db.add(ResourceReservation(id=new_id(), session_id=session.id, trip_id=trip.id, resource_type="STAGING", resource_id=staging.id))
    return True


def _release_trip_resources(db, trip: Trip) -> None:
    rows = list(db.scalars(select(ResourceReservation).where(ResourceReservation.trip_id == trip.id, ResourceReservation.released_at.is_(None))))
    for row in rows:
        if row.resource_type == "DOCK":
            dock = db.get(Dock, row.resource_id)
            if dock is not None and dock.occupied_by == trip.id:
                dock.occupied_by = None
                dock.version += 1
        elif row.resource_type == "STAGING":
            staging = db.get(StagingArea, row.resource_id)
            if staging is not None:
                staging.used_pallets = max(0, staging.used_pallets - trip.reserved_staging_pallets)
                staging.version += 1
        row.released_at = utcnow()
    trip.reserved_dock_id = None
    trip.reserved_agv_id = None
    trip.reserved_staging_id = None
    trip.reserved_staging_pallets = 0


def _plan_jobs(db, plan: Plan) -> list[Job] | None:
    if not isinstance(plan.selected_job_ids, list) or not plan.selected_job_ids or any(not isinstance(value, str) for value in plan.selected_job_ids):
        return None
    if len(set(plan.selected_job_ids)) != len(plan.selected_job_ids):
        return None
    jobs = [db.get(Job, job_id) for job_id in plan.selected_job_ids]
    if any(job is None for job in jobs):
        return None
    return [job for job in jobs if job is not None]


def apply_command(db, session: SimulationSession, command: Command) -> None:
    payload = command.payload
    if not isinstance(payload, dict):
        reject(command, "INVALID_INPUT", {"reason": "payload must be an object"})
        return
    membership = db.scalar(select(Membership).where(Membership.session_id == session.id, Membership.user_id == command.user_id))
    if membership is None:
        reject(command, "MEMBERSHIP_REQUIRED")
        return
    actor = db.get(User, command.user_id)
    host_authorized = membership.role == "HOST" and membership.is_host and actor is not None and actor.is_admin
    if command.type == "CREATE_JOB":
        if membership.role == "HOST" and not host_authorized:
            reject(command, "FORBIDDEN")
            return
        if membership.role not in {"SHIPPER", "HOST"}:
            reject(command, "FORBIDDEN")
            return
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in {"L", "B"}:
            reject(command, "INVALID_INPUT", {"reason": "kind must be L or B"})
            return
        job_id = _required_string(payload, "id") if "id" in payload else new_id()
        if db.get(Job, job_id):
            reject(command, "DUPLICATE_JOB")
            return
        location = _required_string(payload, "location")
        pallets = _integer(payload, "pallets", minimum=1)
        weight_g = _integer(payload, "weight_g", minimum=0)
        earliest_ms = _integer({**payload, "earliest_ms": payload.get("earliest_ms", 0)}, "earliest_ms", minimum=0)
        latest_ms = _integer({**payload, "latest_ms": payload.get("latest_ms", 86_400_000)}, "latest_ms", minimum=0)
        service_ms = _integer({**payload, "service_ms": payload.get("service_ms", 300_000)}, "service_ms", minimum=1)
        revenue_yen = _integer({**payload, "revenue_yen": payload.get("revenue_yen", 0)}, "revenue_yen", minimum=0)
        if earliest_ms > latest_ms:
            reject(command, "INVALID_INPUT", {"reason": "earliest_ms must be <= latest_ms"})
            return
        job = Job(
            id=job_id,
            session_id=session.id,
            kind=kind,
            location=location,
            pallets=pallets,
            weight_g=weight_g,
            earliest_ms=earliest_ms,
            latest_ms=latest_ms,
            service_ms=service_ms,
            revenue_yen=revenue_yen,
        )
        db.add(job)
        command.status = "APPLIED"
        command.result = {"job_id": job.id}
        append_event(db, session, "JOB_CREATED", {"job_id": job.id, "kind": job.kind, "location": job.location})
    elif command.type == "ACCEPT_JOB":
        if membership.role != "DRIVER":
            reject(command, "FORBIDDEN")
            return
        job = db.get(Job, _required_string(payload, "job_id"))
        truck = db.get(Truck, _required_string(payload, "truck_id"))
        if job is None or truck is None or job.session_id != session.id or truck.session_id != session.id:
            reject(command, "NOT_FOUND")
            return
        if job.version != command.expected_entity_version:
            reject(command, "VERSION_CONFLICT", {"current_version": job.version})
            return
        if job.status != "OPEN":
            reject(command, "JOB_NOT_OPEN")
            return
        if truck.assigned_user_id not in {None, command.user_id}:
            reject(command, "TRUCK_NOT_ASSIGNED")
            return
        if truck.status not in {"AVAILABLE", "RESERVED"}:
            reject(command, "TRUCK_IN_USE")
            return
        contracted = list(db.scalars(select(Job).where(Job.session_id == session.id, Job.contracted_truck_id == truck.id, Job.status.in_(["CONTRACTED", "IN_PROGRESS"]))))
        linehaul_pallets = sum(row.pallets for row in contracted if row.kind == "L") + (job.pallets if job.kind == "L" else 0)
        linehaul_weight = sum(row.weight_g for row in contracted if row.kind == "L") + (job.weight_g if job.kind == "L" else 0)
        return_pallets = sum(row.pallets for row in contracted if row.kind == "B") + (job.pallets if job.kind == "B" else 0)
        return_weight = sum(row.weight_g for row in contracted if row.kind == "B") + (job.weight_g if job.kind == "B" else 0)
        if linehaul_pallets > truck.capacity_pallets or linehaul_weight > truck.capacity_weight_g or return_pallets > truck.capacity_pallets or return_weight > truck.capacity_weight_g:
            reject(command, "CAPACITY_EXCEEDED")
            return
        if truck.assigned_user_id is None:
            truck.assigned_user_id = command.user_id
        job.status = "CONTRACTED"
        job.contracted_truck_id = truck.id
        job.version += 1
        truck.status = "RESERVED"
        truck.version += 1
        command.status = "APPLIED"
        command.result = {"job_id": job.id, "truck_id": truck.id}
        append_event(db, session, "JOB_CONTRACTED", {"job_id": job.id, "truck_id": truck.id})
    elif command.type == "START_TRIP":
        if membership.role != "DRIVER":
            reject(command, "FORBIDDEN")
            return
        plan = db.get(Plan, _required_string(payload, "plan_id"))
        truck = db.get(Truck, _required_string(payload, "truck_id"))
        if plan is None or truck is None or plan.session_id != session.id or truck.session_id != session.id:
            reject(command, "NOT_FOUND")
            return
        if plan.truck_id != truck.id or truck.assigned_user_id != command.user_id:
            reject(command, "TRUCK_NOT_ASSIGNED")
            return
        if db.scalar(select(Trip).where(Trip.truck_id == truck.id, Trip.status == "ACTIVE")) is not None:
            reject(command, "TRUCK_ALREADY_IN_TRIP")
            return
        if truck.status not in {"AVAILABLE", "RESERVED"}:
            reject(command, "TRUCK_IN_USE")
            return
        if plan.status != "APPLIED" or plan.base_state_version != session.state_version:
            reject(command, "STALE_PLAN")
            return
        jobs = _plan_jobs(db, plan)
        contracted = list(db.scalars(select(Job).where(Job.session_id == session.id, Job.contracted_truck_id == truck.id, Job.status == "CONTRACTED")))
        if jobs is None or any(job.session_id != session.id for job in jobs) or {job.id for job in jobs} != {job.id for job in contracted} or not contracted:
            reject(command, "PLAN_JOB_SET_CHANGED")
            return
        linehaul_pallets = sum(job.pallets for job in jobs if job.kind == "L")
        linehaul_weight = sum(job.weight_g for job in jobs if job.kind == "L")
        return_pallets = sum(job.pallets for job in jobs if job.kind == "B")
        return_weight = sum(job.weight_g for job in jobs if job.kind == "B")
        if linehaul_pallets > truck.capacity_pallets or linehaul_weight > truck.capacity_weight_g or return_pallets > truck.capacity_pallets or return_weight > truck.capacity_weight_g:
            reject(command, "CAPACITY_EXCEEDED")
            return
        trip = Trip(
            id=new_id(),
            session_id=session.id,
            plan_id=plan.id,
            truck_id=truck.id,
            driver_user_id=command.user_id,
            phase="LOADING",
            next_event_at_ms=session.current_sim_time_ms + LOAD_DURATION_MS,
            outbound_pallets=linehaul_pallets,
            outbound_weight_g=linehaul_weight,
            return_pallets=return_pallets,
            return_weight_g=return_weight,
            started_sim_time_ms=session.current_sim_time_ms,
        )
        db.add(trip)
        db.flush()
        if not _reserve_trip_resources(db, session, trip, staging_pallets=linehaul_pallets):
            db.delete(trip)
            reject(command, "WAREHOUSE_UNAVAILABLE")
            return
        truck.status = "LOADING"
        truck.version += 1
        plan.status = "STARTED"
        command.status = "APPLIED"
        command.result = {"trip_id": trip.id, "plan_id": plan.id, "truck_id": truck.id}
        append_event(db, session, "TRIP_STARTED", {"trip_id": trip.id, "plan_id": plan.id, "truck_id": truck.id})
    elif command.type in {"PAUSE", "RESUME", "SET_SPEED"}:
        if not host_authorized:
            reject(command, "FORBIDDEN")
            return
        if command.type == "PAUSE":
            session.status = "PAUSED"
        elif command.type == "RESUME":
            session.status = "RUNNING"
        else:
            speed = _number({**payload, "speed": payload.get("speed", 1)}, "speed")
            if speed not in {0, 1, 10, 60}:
                reject(command, "INVALID_SPEED")
                return
            session.speed = speed
        command.status = "APPLIED"
        command.result = {"session_status": session.status, "speed": session.speed}
        append_event(db, session, "SESSION_CHANGED", {"status": session.status, "speed": session.speed})
    elif command.type == "SAVE_SNAPSHOT":
        warehouse = WarehouseAdapter(agv_ids=list(AGV_IDS), staging_capacity=40, sim_time_ms=session.current_sim_time_ms, event_seq=session.event_seq)
        snapshot = warehouse.export_snapshot()
        db.add(Snapshot(id=new_id(), session_id=session.id, snapshot_format_version=1, engine_build_id=snapshot["engine_build_id"], last_applied_event_seq=session.event_seq, state=snapshot))
        command.status = "APPLIED"
        command.result = {"event_seq": session.event_seq}
        append_event(db, session, "SNAPSHOT_SAVED", {"last_applied_event_seq": session.event_seq})
    else:
        reject(command, "UNKNOWN_COMMAND")
        return
    command.processed_at = utcnow()


def advance_trips(db, session: SimulationSession) -> None:
    """Advance due trip phases while keeping all state and resource changes transactional."""
    trips = list(db.scalars(select(Trip).where(Trip.session_id == session.id, Trip.status == "ACTIVE").order_by(Trip.id)))
    for trip in trips:
        if trip.next_event_at_ms is not None and trip.next_event_at_ms > session.current_sim_time_ms and trip.phase != "WAITING":
            continue
        plan = db.get(Plan, trip.plan_id)
        jobs = _plan_jobs(db, plan) if plan is not None else None
        if not jobs:
            trip.status = "FAILED"
            trip.phase = "FAILED"
            trip.wait_reason = "PLAN_DATA_UNAVAILABLE"
            _release_trip_resources(db, trip)
            truck = db.get(Truck, trip.truck_id)
            if truck is not None:
                truck.status = "AVAILABLE"
                truck.version += 1
            continue

        def next_job(kind: str, status: str) -> Job | None:
            return next((job for job in jobs if job.kind == kind and job.status == status), None)

        def wait(reason: str) -> None:
            trip.phase = "WAITING"
            trip.wait_reason = reason
            trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
            trip.version += 1

        if trip.phase == "LOADING":
            _release_trip_resources(db, trip)
            trip.phase = "OUTBOUND_TRAVEL"
            trip.wait_reason = None
            trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
            trip.version += 1
            append_event(db, session, "TRIP_LOADING_COMPLETED", {"trip_id": trip.id})
            continue

        if trip.phase == "WAITING":
            if trip.wait_reason and trip.wait_reason.startswith("UNLOAD"):
                if not _reserve_trip_resources(db, session, trip):
                    wait(trip.wait_reason)
                    continue
                trip.phase = "UNLOADING"
                trip.wait_reason = None
                trip.next_event_at_ms = session.current_sim_time_ms + UNLOAD_DURATION_MS
                trip.version += 1
                continue
            elif trip.current_job_id:
                current = db.get(Job, trip.current_job_id)
                trip.phase = "OUTBOUND_TRAVEL" if current is not None and current.kind == "L" else "RETURN_TRAVEL"
            else:
                trip.phase = "RETURN_TRAVEL"

        if trip.phase == "OUTBOUND_TRAVEL":
            current = db.get(Job, trip.current_job_id) if trip.current_job_id else next_job("L", "CONTRACTED")
            if current is None:
                trip.phase = "RETURN_TRAVEL"
                trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
                trip.version += 1
                continue
            trip.current_job_id = current.id
            if not _reserve_trip_resources(db, session, trip):
                wait("HANDLING_RESOURCES_UNAVAILABLE")
                continue
            trip.phase = "OUTBOUND_HANDLING"
            trip.wait_reason = None
            trip.next_event_at_ms = session.current_sim_time_ms + max(1, current.service_ms)
            trip.version += 1
            append_event(db, session, "TRIP_ARRIVED_DELIVERY", {"trip_id": trip.id, "job_id": current.id})
            continue

        if trip.phase == "OUTBOUND_HANDLING":
            current = db.get(Job, trip.current_job_id) if trip.current_job_id else None
            if current is not None:
                current.status = "COMPLETED"
                current.version += 1
            _release_trip_resources(db, trip)
            trip.current_job_id = None
            next_linehaul = next_job("L", "CONTRACTED")
            if next_linehaul is not None:
                trip.current_job_id = next_linehaul.id
                trip.phase = "OUTBOUND_TRAVEL"
            else:
                next_return = next_job("B", "CONTRACTED")
                if next_return is not None:
                    trip.current_job_id = next_return.id
                trip.phase = "RETURN_TRAVEL"
            trip.wait_reason = None
            trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
            trip.version += 1
            append_event(db, session, "DELIVERY_COMPLETED", {"trip_id": trip.id, "job_id": current.id if current else None})
            continue

        if trip.phase == "RETURN_TRAVEL":
            current = db.get(Job, trip.current_job_id) if trip.current_job_id else next_job("B", "CONTRACTED")
            if current is None:
                if not _reserve_trip_resources(db, session, trip):
                    wait("UNLOAD_RESOURCES_UNAVAILABLE")
                    continue
                trip.phase = "UNLOADING"
                trip.wait_reason = None
                trip.next_event_at_ms = session.current_sim_time_ms + UNLOAD_DURATION_MS
                trip.version += 1
                continue
            trip.current_job_id = current.id
            if not _reserve_trip_resources(db, session, trip):
                wait("RETURN_HANDLING_RESOURCES_UNAVAILABLE")
                continue
            current.status = "IN_PROGRESS"
            current.version += 1
            trip.phase = "RETURN_HANDLING"
            trip.wait_reason = None
            trip.next_event_at_ms = session.current_sim_time_ms + max(1, current.service_ms)
            trip.version += 1
            append_event(db, session, "TRIP_PICKUP_STARTED", {"trip_id": trip.id, "job_id": current.id})
            continue

        if trip.phase == "RETURN_HANDLING":
            current = db.get(Job, trip.current_job_id) if trip.current_job_id else None
            _release_trip_resources(db, trip)
            trip.current_job_id = None
            next_return = next_job("B", "CONTRACTED")
            if next_return is not None:
                trip.current_job_id = next_return.id
            trip.phase = "RETURN_TRAVEL"
            trip.wait_reason = None
            trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
            trip.version += 1
            append_event(db, session, "RETURN_PICKUP_COMPLETED", {"trip_id": trip.id, "job_id": current.id if current else None})
            continue

        if trip.phase == "UNLOADING":
            pending_linehaul = next((job for job in jobs if job.kind == "L" and job.status != "COMPLETED"), None)
            if pending_linehaul is not None:
                _release_trip_resources(db, trip)
                trip.current_job_id = pending_linehaul.id
                trip.phase = "OUTBOUND_TRAVEL"
                trip.wait_reason = None
                trip.next_event_at_ms = session.current_sim_time_ms + TRAVEL_DURATION_MS
                trip.version += 1
                continue
            for job in jobs:
                if job.kind == "B" and job.status == "IN_PROGRESS":
                    job.status = "COMPLETED"
                    job.version += 1
            _release_trip_resources(db, trip)
            trip.status = "COMPLETED"
            trip.phase = "COMPLETED"
            trip.wait_reason = None
            trip.next_event_at_ms = None
            trip.completed_sim_time_ms = session.current_sim_time_ms
            trip.version += 1
            if plan is not None:
                plan.status = "COMPLETED"
            truck = db.get(Truck, trip.truck_id)
            if truck is not None:
                truck.status = "AVAILABLE"
                truck.version += 1
            append_event(db, session, "TRIP_COMPLETED", {"trip_id": trip.id, "plan_id": trip.plan_id})


def process_commands(session_id: str, generation: int) -> None:
    try:
        with SessionLocal() as db:
            session = db.get(SimulationSession, session_id)
            if session is None or session.owner_id != WORKER_ID or session.worker_generation != generation:
                return
            session.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
            command = db.scalar(select(Command).where(Command.session_id == session_id, Command.status == "ACCEPTED").order_by(Command.created_at, Command.id).with_for_update())
            if command is None:
                db.commit()
                return
            try:
                apply_command(db, session, command)
                db.commit()
            except (InvalidCommand, TypeError, ValueError, OverflowError) as exc:
                db.rollback()
                failed = db.get(Command, command.id)
                if failed is not None and failed.status == "ACCEPTED":
                    reject(failed, "INVALID_INPUT", {"reason": str(exc)})
                db.commit()
            except Exception:
                db.rollback()
                failed = db.get(Command, command.id)
                if failed is not None and failed.status == "ACCEPTED":
                    reject(failed, "WORKER_COMMAND_ERROR")
                db.commit()
    except Exception:
        # A malformed queue item or a transient database error must not kill the loop.
        return


def advance_clock(session_id: str, generation: int) -> None:
    with SessionLocal() as db:
        session = db.get(SimulationSession, session_id)
        if session is None or session.owner_id != WORKER_ID or session.worker_generation != generation or session.status != "RUNNING":
            return
        session.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
        session.current_sim_time_ms += int(200 * max(0, session.speed))
        advance_trips(db, session)
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
            try:
                process_commands(session_id, generation)
                advance_clock(session_id, generation)
            except Exception:
                continue
        try:
            with SessionLocal() as db:
                process_outbox(db)
        except Exception:
            pass
        try:
            dispatch_plans()
        except Exception:
            pass
        time.sleep(0.2)


if __name__ == "__main__":
    main()
