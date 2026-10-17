from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Base, SessionLocal, engine, get_db, wait_for_database
from .models import (
    AuthSession,
    Command,
    Dock,
    DomainEvent,
    Job,
    Membership,
    Outbox,
    Plan,
    SimulationSession,
    Snapshot,
    StagingArea,
    StreamNotification,
    Truck,
    User,
    utcnow,
)


def uid() -> str:
    return str(uuid.uuid4())


def password_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LoginRequest(BaseModel):
    username: str
    password: str


class JoinRequest(BaseModel):
    role: str = Field(pattern="^(SHIPPER|DRIVER|WAREHOUSE|HOST)$")


class CommandRequest(BaseModel):
    type: str
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_entity_version: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class PlanRequest(BaseModel):
    truck_id: str | None = None
    time_limit_ms: int = 5_000


def user_from_token(db: Session, token: str | None) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    auth = db.get(AuthSession, token)
    expires_at = auth.expires_at.replace(tzinfo=timezone.utc) if auth and auth.expires_at.tzinfo is None else (auth.expires_at if auth else None)
    if auth is None or expires_at < utcnow():
        raise HTTPException(status_code=401, detail="authentication required")
    user = db.get(User, auth.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def current_user(request: Request, slap_session: str | None = Cookie(default=None), db: Session = Depends(get_db)) -> User:
    return user_from_token(db, slap_session or request.headers.get("x-session-token"))


def get_membership(db: Session, session_id: str, user_id: str) -> Membership:
    membership = db.scalar(select(Membership).where(Membership.session_id == session_id, Membership.user_id == user_id))
    if membership is None:
        raise HTTPException(status_code=403, detail="session membership required")
    return membership


def require_session(db: Session, session_id: str, user: User) -> tuple[SimulationSession, Membership]:
    session = db.get(SimulationSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session, get_membership(db, session_id, user.id)


def serialize_job(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "location": job.location,
        "pallets": job.pallets,
        "weight_g": job.weight_g,
        "earliest_ms": job.earliest_ms,
        "latest_ms": job.latest_ms,
        "service_ms": job.service_ms,
        "revenue_yen": job.revenue_yen,
        "status": job.status,
        "contracted_truck_id": job.contracted_truck_id,
        "version": job.version,
    }


def serialize_state(db: Session, session: SimulationSession, membership: Membership) -> dict[str, Any]:
    jobs = list(db.scalars(select(Job).where(Job.session_id == session.id).order_by(Job.kind, Job.id)))
    trucks = list(db.scalars(select(Truck).where(Truck.session_id == session.id).order_by(Truck.name)))
    docks = list(db.scalars(select(Dock).where(Dock.session_id == session.id).order_by(Dock.name)))
    staging = list(db.scalars(select(StagingArea).where(StagingArea.session_id == session.id).order_by(StagingArea.name)))
    visible_jobs = jobs if membership.role in {"HOST", "WAREHOUSE"} else [job for job in jobs if job.status in {"OPEN", "CONTRACTED", "IN_PROGRESS", "COMPLETED"}]
    return {
        "session": {
            "id": session.id,
            "name": session.name,
            "status": session.status,
            "speed": session.speed,
            "current_sim_time_ms": session.current_sim_time_ms,
            "state_version": session.state_version,
            "event_seq": session.event_seq,
            "worker_generation": session.worker_generation,
        },
        "membership": {"role": membership.role, "is_host": membership.is_host, "is_warehouse_operator": membership.is_warehouse_operator},
        "jobs": [serialize_job(job) for job in visible_jobs],
        "trucks": [
            {
                "id": truck.id,
                "name": truck.name,
                "status": truck.status,
                "capacity_pallets": truck.capacity_pallets,
                "capacity_weight_g": truck.capacity_weight_g,
                "assigned_user_id": truck.assigned_user_id,
                "version": truck.version,
            }
            for truck in trucks
        ],
        "docks": [{"id": dock.id, "name": dock.name, "occupied_by": dock.occupied_by, "version": dock.version} for dock in docks],
        "staging_areas": [{"id": area.id, "name": area.name, "capacity_pallets": area.capacity_pallets, "used_pallets": area.used_pallets} for area in staging],
    }


def publish_outbox(db: Session, session_id: str) -> None:
    memberships = list(db.scalars(select(Membership).where(Membership.session_id == session_id)))
    outbox_rows = list(db.scalars(select(Outbox).where(Outbox.session_id == session_id, Outbox.processed_at.is_(None)).order_by(Outbox.id).limit(200)))
    for row in outbox_rows:
        for membership in memberships:
            last_seq = db.scalar(select(func.max(StreamNotification.seq)).where(StreamNotification.session_id == session_id, StreamNotification.user_id == membership.user_id)) or 0
            notification = StreamNotification(
                id=uid(),
                session_id=session_id,
                user_id=membership.user_id,
                seq=int(last_seq) + 1,
                event_seq=int(row.payload.get("event_seq", 0)),
                state_version=int(row.payload.get("state_version", 0)),
                sim_time_ms=int(row.payload.get("sim_time_ms", 0)),
                type=row.type,
                payload=row.payload,
            )
            db.add(notification)
        row.processed_at = utcnow()
    if outbox_rows:
        db.commit()


def seed_database() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        users = {
            "admin": ("admin", True),
            "shipper": ("shipper", False),
            "driver": ("driver", False),
        }
        user_map: dict[str, User] = {}
        for username, (password, is_admin) in users.items():
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                user = User(id=uid(), username=username, password_hash=password_hash(password), display_name=username.title(), is_admin=is_admin)
                db.add(user)
                db.flush()
            user_map[username] = user
        sim = db.get(SimulationSession, "demo-session")
        if sim is None:
            sim = SimulationSession(id="demo-session", name="SLAPStack VRPB Demo", status="RUNNING", speed=1.0, state_version=1)
            db.add(sim)
            db.flush()
            db.add_all([
                Membership(id=uid(), session_id=sim.id, user_id=user_map["admin"].id, role="HOST", is_host=True, is_warehouse_operator=True),
                Membership(id=uid(), session_id=sim.id, user_id=user_map["shipper"].id, role="SHIPPER"),
                Membership(id=uid(), session_id=sim.id, user_id=user_map["driver"].id, role="DRIVER"),
            ])
            db.add_all([
                Truck(id="truck-1", session_id=sim.id, name="Truck 01", capacity_pallets=20, capacity_weight_g=20_000),
                Truck(id="truck-2", session_id=sim.id, name="Truck 02", capacity_pallets=20, capacity_weight_g=20_000),
            ])
            db.add_all([Dock(id=f"dock-{i}", session_id=sim.id, name=f"Bay {i}") for i in range(1, 5)])
            db.add(StagingArea(id="staging-1", session_id=sim.id, name="Staging", capacity_pallets=40))
            db.add_all([
                Job(id="job-linehaul-1", session_id=sim.id, kind="L", location="Nagoya", pallets=4, weight_g=4_000, earliest_ms=0, latest_ms=18_000_000, revenue_yen=30_000),
                Job(id="job-backhaul-1", session_id=sim.id, kind="B", location="Gifu", pallets=3, weight_g=3_000, earliest_ms=18_000_000, latest_ms=21_600_000, revenue_yen=22_000, status="OPEN"),
            ])
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_database()
    seed_database()
    yield


app = FastAPI(title="SLAPStack VRPB API", version="1.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/api/v1/auth/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or user.password_hash != password_hash(body.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.now(timezone.utc) + timedelta(hours=12)))
    db.commit()
    response.set_cookie("slap_session", token, httponly=True, samesite="lax")
    session = db.get(SimulationSession, "demo-session")
    return {"user_id": user.id, "username": user.username, "session_id": session.id if session else None, "token": token}


@app.get("/api/v1/sessions")
def list_sessions(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(SimulationSession).order_by(desc(SimulationSession.created_at))))
    return [{"id": row.id, "name": row.name, "status": row.status, "state_version": row.state_version} for row in rows]


@app.post("/api/v1/sessions/{session_id}/join")
def join(session_id: str, body: JoinRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session = db.get(SimulationSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    existing = db.scalar(select(Membership).where(Membership.session_id == session_id, Membership.user_id == user.id))
    if existing:
        existing.role = body.role
        existing.is_host = body.role == "HOST" or user.is_admin
        existing.is_warehouse_operator = body.role in {"HOST", "WAREHOUSE"}
        db.commit()
        return {"membership_id": existing.id, "role": existing.role, "session_id": session_id}
    membership = Membership(id=uid(), session_id=session_id, user_id=user.id, role=body.role, is_host=body.role == "HOST" or user.is_admin, is_warehouse_operator=body.role in {"HOST", "WAREHOUSE"})
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="membership already exists")
    return {"membership_id": membership.id, "role": membership.role, "session_id": session_id}


@app.get("/api/v1/sessions/{session_id}/state")
def state(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session, membership = require_session(db, session_id, user)
    publish_outbox(db, session_id)
    return serialize_state(db, session, membership)


@app.post("/api/v1/sessions/{session_id}/commands", status_code=202)
def create_command(session_id: str, body: CommandRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session, membership = require_session(db, session_id, user)
    if body.type in {"CREATE_JOB", "ACCEPT_JOB"} and membership.role not in {"SHIPPER", "DRIVER", "HOST"}:
        raise HTTPException(status_code=403, detail="role cannot submit this command")
    existing = db.scalar(select(Command).where(Command.session_id == session_id, Command.user_id == user.id, Command.idempotency_key == body.idempotency_key))
    if existing:
        if existing.type != body.type or existing.payload != body.payload:
            raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
        return {"command_id": existing.id, "status": existing.status}
    command = Command(id=uid(), session_id=session_id, user_id=user.id, idempotency_key=body.idempotency_key, type=body.type, payload=body.payload, expected_entity_version=body.expected_entity_version)
    db.add(command)
    try:
        db.flush()
        if db.bind and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_notify('commands', :payload)"), {"payload": json.dumps({"session_id": session_id, "command_id": command.id})})
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Command).where(Command.session_id == session_id, Command.user_id == user.id, Command.idempotency_key == body.idempotency_key))
        if existing:
            return {"command_id": existing.id, "status": existing.status}
        raise HTTPException(status_code=409, detail="command conflict")
    return {"command_id": command.id, "status": command.status}


@app.get("/api/v1/commands/{command_id}")
def command_status(command_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    command = db.get(Command, command_id)
    if command is None or command.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=404, detail="command not found")
    return {"command_id": command.id, "status": command.status, "rejection_reason": command.rejection_reason, "result": command.result}


@app.post("/api/v1/sessions/{session_id}/plans", status_code=202)
def create_plan(session_id: str, body: PlanRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session, membership = require_session(db, session_id, user)
    if membership.role not in {"DRIVER", "HOST", "WAREHOUSE"}:
        raise HTTPException(status_code=403, detail="driver or host role required")
    plan = Plan(id=uid(), session_id=session_id, base_state_version=session.state_version)
    db.add(plan)
    db.commit()
    # The optimizer is a separate service; the worker/optimizer poller can recover this request.
    return {"plan_id": plan.id, "status": plan.status, "base_state_version": plan.base_state_version, "optimizer_url": os.getenv("OPTIMIZER_URL", "http://optimizer:8001")}


@app.get("/api/v1/sessions/{session_id}/plans/{plan_id}")
def get_plan(session_id: str, plan_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _, _ = require_session(db, session_id, user)
    plan = db.get(Plan, plan_id)
    if plan is None or plan.session_id != session_id:
        raise HTTPException(status_code=404, detail="plan not found")
    return {"id": plan.id, "status": plan.status, "base_state_version": plan.base_state_version, "solution_status": plan.solution_status, "termination_reason": plan.termination_reason, "route": plan.route, "selected_job_ids": plan.selected_job_ids, "objective_value": plan.objective_value, "reason": plan.reason}


@app.websocket("/ws/v1/sessions/{session_id}")
async def websocket_stream(websocket: WebSocket, session_id: str, db: Session = Depends(get_db)) -> None:
    token = websocket.cookies.get("slap_session") or websocket.query_params.get("token")
    try:
        user = user_from_token(db, token)
        membership = get_membership(db, session_id, user.id)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    last_seq = int(websocket.query_params.get("last_seq", "0"))
    try:
        while True:
            publish_outbox(db, session_id)
            notifications = list(db.scalars(select(StreamNotification).where(StreamNotification.session_id == session_id, StreamNotification.user_id == user.id, StreamNotification.seq > last_seq).order_by(StreamNotification.seq).limit(100)))
            for notification in notifications:
                await websocket.send_json({"seq": notification.seq, "event_seq": notification.event_seq, "sim_time": notification.sim_time_ms, "state_version": notification.state_version, "type": notification.type, "payload": notification.payload})
                last_seq = notification.seq
            await websocket.send_json({"type": "heartbeat", "seq": last_seq})
            await __import__("asyncio").sleep(0.2)
            db.expire_all()
    except WebSocketDisconnect:
        return
