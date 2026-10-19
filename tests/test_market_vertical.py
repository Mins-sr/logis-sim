from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Base, SessionLocal, engine
from app.main import JoinRequest, app, join, seed_database
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
    StagingArea,
    StreamNotification,
    Truck,
    Trip,
    User,
    utcnow,
)
import optimizer.main as optimizer_module
from optimizer.main import calculate
from shared.vrpb import VRPBResult
from worker.main import AGV_IDS, WORKER_ID, advance_trips, apply_command, process_outbox, process_commands


@pytest.fixture(autouse=True)
def clean_market_db():
    Base.metadata.drop_all(bind=engine)
    seed_database()
    yield


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["session_id"]


def _command(db, session_id: str, user_id: str, command_type: str, payload: dict, expected: int = 0) -> Command:
    command = Command(
        id=str(uuid.uuid4()),
        session_id=session_id,
        user_id=user_id,
        idempotency_key=str(uuid.uuid4()),
        type=command_type,
        payload=payload,
        expected_entity_version=expected,
    )
    db.add(command)
    db.flush()
    session = db.get(SimulationSession, session_id)
    assert session is not None
    apply_command(db, session, command)
    db.commit()
    db.refresh(command)
    return command


def _contract_seed_jobs(db, session_id: str, driver_id: str, truck_id: str = "truck-1") -> tuple[Job, Job, Truck]:
    linehaul = db.get(Job, "job-linehaul-1")
    backhaul = db.get(Job, "job-backhaul-1")
    truck = db.get(Truck, truck_id)
    assert linehaul and backhaul and truck
    assert _command(db, session_id, driver_id, "ACCEPT_JOB", {"job_id": linehaul.id, "truck_id": truck.id}).status == "APPLIED"
    assert _command(db, session_id, driver_id, "ACCEPT_JOB", {"job_id": backhaul.id, "truck_id": truck.id}).status == "APPLIED"
    return linehaul, backhaul, truck


def _make_plan(db, session_id: str, truck_id: str, job_ids: list[str]) -> Plan:
    session = db.get(SimulationSession, session_id)
    assert session is not None
    plan = Plan(
        id=str(uuid.uuid4()),
        session_id=session_id,
        truck_id=truck_id,
        base_state_version=session.state_version,
        base_sim_time_ms=session.current_sim_time_ms,
        time_limit_ms=5_000,
        calculation_deadline_at=utcnow() + timedelta(seconds=5),
        status="PROPOSED",
        selected_job_ids=job_ids,
    )
    db.add(plan)
    db.commit()
    return plan


def test_worker_outbox_assigns_monotonic_sequences_and_api_is_read_only():
    with TestClient(app) as client:
        session_id = _login(client, "admin", "admin")
        with SessionLocal() as db:
            session = db.get(SimulationSession, session_id)
            admin = db.scalar(select(User).where(User.username == "admin"))
            assert session and admin
            for seq in (1, 2):
                event = DomainEvent(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    event_seq=seq,
                    sim_time_ms=seq,
                    state_version=seq,
                    type=f"TEST_{seq}",
                    payload={"event_seq": seq, "state_version": seq, "sim_time_ms": seq},
                )
                db.add(event)
                db.add(Outbox(id=str(uuid.uuid4()), session_id=session_id, event_id=event.id, type=event.type, payload=event.payload))
            db.commit()

        assert client.get(f"/api/v1/sessions/{session_id}/state").status_code == 200
        with SessionLocal() as db:
            assert db.scalar(select(func.count(StreamNotification.id))) == 0
            process_outbox(db, session_id)
            process_outbox(db, session_id)
            rows = list(db.scalars(select(StreamNotification).where(StreamNotification.user_id == admin.id).order_by(StreamNotification.seq)))
            assert [row.seq for row in rows] == [1, 2]
            assert len({row.event_id for row in rows}) == 2


def test_host_requires_admin_is_unique_and_warehouse_role_is_rejected():
    with TestClient(app) as client:
        session_id = _login(client, "driver", "driver")
        assert client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "HOST"}).status_code == 403
        assert client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "WAREHOUSE"}).status_code == 422
        assert client.post(f"/api/v1/sessions/{session_id}/join", json={"role": "DRIVER"}).status_code == 200

        with SessionLocal() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            second_admin = User(id=str(uuid.uuid4()), username="admin-2", password_hash="x", display_name="Admin 2", is_admin=True)
            db.add(second_admin)
            db.commit()
            with pytest.raises(HTTPException) as error:
                join(session_id, JoinRequest(role="HOST"), user=second_admin, db=db)
            assert error.value.status_code == 409
            assert admin is not None


def test_accept_job_assigns_only_requested_truck_without_loading_and_checks_both_capacities():
    with SessionLocal() as db:
        session = db.get(SimulationSession, "demo-session")
        driver = db.scalar(select(User).where(User.username == "driver"))
        truck = db.get(Truck, "truck-1")
        assert session and driver and truck
        linehaul, _, _ = _contract_seed_jobs(db, session.id, driver.id)
        assert truck.assigned_user_id == driver.id
        assert truck.status == "RESERVED"
        assert linehaul.contracted_truck_id == truck.id
        assert db.scalar(select(func.count(ResourceReservation.id)).where(ResourceReservation.released_at.is_(None))) == 0

        too_much_outbound = Job(id="too-many-outbound", session_id=session.id, kind="L", location="L2", pallets=17, weight_g=1)
        too_much_return = Job(id="too-many-return", session_id=session.id, kind="B", location="B2", pallets=18, weight_g=1)
        db.add_all([too_much_outbound, too_much_return])
        db.commit()
        assert _command(db, session.id, driver.id, "ACCEPT_JOB", {"job_id": too_much_outbound.id, "truck_id": truck.id}).rejection_reason == "CAPACITY_EXCEEDED"
        assert _command(db, session.id, driver.id, "ACCEPT_JOB", {"job_id": too_much_return.id, "truck_id": truck.id}).rejection_reason == "CAPACITY_EXCEEDED"


def test_plan_requires_truck_and_optimizer_only_uses_that_trucks_contracts_with_duplicate_locations():
    with TestClient(app) as client:
        session_id = _login(client, "driver", "driver")
        missing = client.post(f"/api/v1/sessions/{session_id}/plans", json={})
        assert missing.status_code == 422

    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        assert driver
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        duplicate_location = Job(id="same-place", session_id="demo-session", kind="B", location=backhaul.location, pallets=1, weight_g=1, status="OPEN")
        db.add(duplicate_location)
        plan = _make_plan(db, "demo-session", truck.id, [linehaul.id, backhaul.id])
        db.commit()
        asyncio.run(calculate(plan.id))
        db.refresh(plan)
        assert plan.status == "APPLIED"
        assert set(plan.selected_job_ids or []) == {linehaul.id, backhaul.id}
        assert duplicate_location.id not in (plan.selected_job_ids or [])
        assert plan.truck_id == truck.id
        assert plan.base_sim_time_ms == 0
        assert plan.calculation_deadline_at is not None


def test_start_trip_checks_plan_ownership_idempotency_and_releases_resources_after_completion():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        assert driver
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        plan = _make_plan(db, "demo-session", truck.id, [linehaul.id, backhaul.id])
        db.commit()
        asyncio.run(calculate(plan.id))
        db.refresh(plan)
        assert plan.status == "APPLIED"

        start = _command(db, "demo-session", driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id})
        assert start.status == "APPLIED"
        trip = db.scalar(select(Trip).where(Trip.plan_id == plan.id))
        assert trip and trip.status == "ACTIVE"
        assert db.scalar(select(func.count(ResourceReservation.id))) >= 2
        assert _command(db, "demo-session", driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id}).rejection_reason == "TRUCK_ALREADY_IN_TRIP"

        for _ in range(20):
            db.refresh(trip)
            if trip.status == "COMPLETED":
                break
            session = db.get(SimulationSession, "demo-session")
            assert session
            session.current_sim_time_ms = max(session.current_sim_time_ms, trip.next_event_at_ms or session.current_sim_time_ms)
            advance_trips(db, session)
            db.commit()
        db.refresh(trip)
        db.refresh(truck)
        db.refresh(linehaul)
        db.refresh(backhaul)
        assert trip.status == "COMPLETED"
        assert truck.status == "AVAILABLE"
        assert linehaul.status == "COMPLETED"
        assert backhaul.status == "COMPLETED"
        assert db.scalar(select(func.count(ResourceReservation.id)).where(ResourceReservation.released_at.is_(None))) == 0


def test_advance_trips_completes_all_linehaul_jobs_before_return_leg_or_trip_completion():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        session = db.get(SimulationSession, "demo-session")
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        second_linehaul = Job(
            id="job-linehaul-2",
            session_id=session.id,
            kind="L",
            location="Osaka",
            pallets=2,
            weight_g=2_000,
            status="OPEN",
        )
        db.add(second_linehaul)
        db.commit()
        assert _command(db, session.id, driver.id, "ACCEPT_JOB", {"job_id": second_linehaul.id, "truck_id": truck.id}).status == "APPLIED"
        plan = _make_plan(db, session.id, truck.id, [linehaul.id, second_linehaul.id, backhaul.id])
        plan.status = "APPLIED"
        db.commit()

        assert _command(db, session.id, driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id}).status == "APPLIED"
        trip = db.scalar(select(Trip).where(Trip.plan_id == plan.id))
        assert trip is not None
        for _ in range(32):
            db.refresh(trip)
            if trip.status == "COMPLETED":
                break
            session = db.get(SimulationSession, session.id)
            assert session is not None
            session.current_sim_time_ms = max(session.current_sim_time_ms, trip.next_event_at_ms or session.current_sim_time_ms)
            advance_trips(db, session)
            db.commit()

        events = list(db.scalars(select(DomainEvent).where(DomainEvent.session_id == session.id).order_by(DomainEvent.event_seq)))
        delivery_ids = [event.payload.get("job_id") for event in events if event.type == "DELIVERY_COMPLETED"]
        pickup_started = [event for event in events if event.type == "TRIP_PICKUP_STARTED"]
        assert delivery_ids == [linehaul.id, second_linehaul.id]
        assert pickup_started
        assert max(event.event_seq for event in events if event.type == "DELIVERY_COMPLETED" and event.payload.get("job_id") == second_linehaul.id) < pickup_started[0].event_seq
        assert trip.status == "COMPLETED"
        assert linehaul.status == "COMPLETED"
        assert second_linehaul.status == "COMPLETED"
        assert backhaul.status == "COMPLETED"


def _block_dock_and_agv_resources(db, session_id: str, blocker_id: str) -> None:
    for dock in db.scalars(select(Dock).where(Dock.session_id == session_id)):
        dock.occupied_by = blocker_id
        db.add(ResourceReservation(id=str(uuid.uuid4()), session_id=session_id, trip_id=blocker_id, resource_type="DOCK", resource_id=dock.id))
    for agv_id in AGV_IDS:
        db.add(ResourceReservation(id=str(uuid.uuid4()), session_id=session_id, trip_id=blocker_id, resource_type="AGV", resource_id=agv_id))


def _release_blocked_resources(db, session_id: str, blocker_id: str) -> None:
    for dock in db.scalars(select(Dock).where(Dock.session_id == session_id)):
        if dock.occupied_by == blocker_id:
            dock.occupied_by = None
    for reservation in db.scalars(select(ResourceReservation).where(ResourceReservation.session_id == session_id, ResourceReservation.trip_id == blocker_id, ResourceReservation.released_at.is_(None))):
        reservation.released_at = utcnow()


def test_waiting_trip_re_reserves_after_resource_release_before_handling():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        session = db.get(SimulationSession, "demo-session")
        linehaul, backhaul, truck = _contract_seed_jobs(db, session.id, driver.id)
        plan = _make_plan(db, session.id, truck.id, [linehaul.id, backhaul.id])
        plan.status = "APPLIED"
        db.commit()
        assert _command(db, session.id, driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id}).status == "APPLIED"
        trip = db.scalar(select(Trip).where(Trip.plan_id == plan.id))
        assert trip is not None

        session.current_sim_time_ms = trip.next_event_at_ms
        advance_trips(db, session)
        db.commit()
        db.refresh(trip)
        assert trip.phase == "OUTBOUND_TRAVEL"

        blocker_id = "resource-blocker-outbound"
        _block_dock_and_agv_resources(db, session.id, blocker_id)
        db.commit()
        session.current_sim_time_ms = trip.next_event_at_ms
        advance_trips(db, session)
        db.commit()
        db.refresh(trip)
        assert trip.phase == "WAITING"
        assert trip.wait_reason == "HANDLING_RESOURCES_UNAVAILABLE"
        assert db.scalar(select(ResourceReservation).where(ResourceReservation.trip_id == trip.id, ResourceReservation.released_at.is_(None))) is None

        _release_blocked_resources(db, session.id, blocker_id)
        db.commit()
        session.current_sim_time_ms = trip.next_event_at_ms
        advance_trips(db, session)
        db.commit()
        db.refresh(trip)
        assert trip.phase == "OUTBOUND_HANDLING"
        assert trip.status == "ACTIVE"
        assert db.scalar(select(ResourceReservation).where(ResourceReservation.trip_id == trip.id, ResourceReservation.released_at.is_(None))) is not None


def test_waiting_unload_re_reserves_before_unloading_or_completion():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        session = db.get(SimulationSession, "demo-session")
        linehaul, backhaul, truck = _contract_seed_jobs(db, session.id, driver.id)
        plan = _make_plan(db, session.id, truck.id, [linehaul.id, backhaul.id])
        plan.status = "APPLIED"
        db.commit()
        assert _command(db, session.id, driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id}).status == "APPLIED"
        trip = db.scalar(select(Trip).where(Trip.plan_id == plan.id))
        assert trip is not None

        linehaul.status = "COMPLETED"
        backhaul.status = "COMPLETED"
        trip.phase = "RETURN_TRAVEL"
        trip.current_job_id = None
        trip.next_event_at_ms = session.current_sim_time_ms
        db.commit()
        _release_blocked_resources(db, session.id, trip.id)
        _release_blocked_resources(db, session.id, "resource-blocker-unload")
        _block_dock_and_agv_resources(db, session.id, "resource-blocker-unload")
        db.commit()

        advance_trips(db, session)
        db.commit()
        db.refresh(trip)
        assert trip.phase == "WAITING"
        assert trip.wait_reason == "UNLOAD_RESOURCES_UNAVAILABLE"

        _release_blocked_resources(db, session.id, "resource-blocker-unload")
        db.commit()
        session.current_sim_time_ms = trip.next_event_at_ms
        advance_trips(db, session)
        db.commit()
        db.refresh(trip)
        assert trip.phase == "UNLOADING"
        assert trip.status == "ACTIVE"
        assert db.scalar(select(ResourceReservation).where(ResourceReservation.trip_id == trip.id, ResourceReservation.released_at.is_(None))) is not None
        assert backhaul.status == "COMPLETED"


def test_same_tick_trip_reservations_are_flushed_before_the_next_trip_reserves():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        session = db.get(SimulationSession, "demo-session")
        db.add(StagingArea(id="staging-2", session_id=session.id, name="Staging 2", capacity_pallets=40))
        db.commit()
        first_linehaul, first_backhaul, first_truck = _contract_seed_jobs(db, session.id, driver.id, "truck-1")
        second_linehaul = Job(id="job-linehaul-2", session_id=session.id, kind="L", location="Osaka", pallets=2, weight_g=2_000, status="OPEN")
        second_backhaul = Job(id="job-backhaul-2", session_id=session.id, kind="B", location="Kyoto", pallets=1, weight_g=1_000, status="OPEN")
        db.add_all([second_linehaul, second_backhaul])
        db.commit()
        second_truck = db.get(Truck, "truck-2")
        assert second_truck is not None
        assert _command(db, session.id, driver.id, "ACCEPT_JOB", {"job_id": second_linehaul.id, "truck_id": second_truck.id}).status == "APPLIED"
        assert _command(db, session.id, driver.id, "ACCEPT_JOB", {"job_id": second_backhaul.id, "truck_id": second_truck.id}).status == "APPLIED"
        first_plan = _make_plan(db, session.id, first_truck.id, [first_linehaul.id, first_backhaul.id])
        second_plan = _make_plan(db, session.id, second_truck.id, [second_linehaul.id, second_backhaul.id])
        first_plan.status = "APPLIED"
        second_plan.status = "APPLIED"
        db.commit()
        assert _command(db, session.id, driver.id, "START_TRIP", {"plan_id": first_plan.id, "truck_id": first_truck.id}).status == "APPLIED"
        session = db.get(SimulationSession, session.id)
        assert session is not None
        second_plan.base_state_version = session.state_version
        db.commit()
        second_start = _command(db, session.id, driver.id, "START_TRIP", {"plan_id": second_plan.id, "truck_id": second_truck.id})
        assert second_start.status == "APPLIED", second_start.rejection_reason
        trips = list(db.scalars(select(Trip).where(Trip.session_id == session.id).order_by(Trip.id)))
        assert len(trips) == 2

        session.current_sim_time_ms = max(trip.next_event_at_ms or 0 for trip in trips)
        advance_trips(db, session)
        db.commit()
        session.current_sim_time_ms = max(trip.next_event_at_ms or 0 for trip in trips)
        advance_trips(db, session)
        db.commit()

        active = list(db.scalars(select(ResourceReservation).where(ResourceReservation.session_id == session.id, ResourceReservation.released_at.is_(None))))
        reserved = {(row.resource_type, row.resource_id) for row in active}
        assert len(reserved) == len(active)
        assert all(trip.phase == "OUTBOUND_HANDLING" for trip in trips)


def test_start_trip_allows_delayed_applied_plan_after_calculation_deadline():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        plan = _make_plan(db, "demo-session", truck.id, [linehaul.id, backhaul.id])
        plan.status = "APPLIED"
        plan.calculation_deadline_at = utcnow() - timedelta(seconds=1)
        db.commit()

        started = _command(db, "demo-session", driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id})
        assert started.status == "APPLIED"


def test_optimizer_matrix_uses_job_locations_not_job_ids(monkeypatch):
    captured: dict[str, object] = {}

    def fake_solve(inp):
        captured["input"] = inp
        return VRPBResult(
            solution_status="FEASIBLE",
            termination_reason="COMPLETED",
            route=[inp.depot, *(job.location for job in inp.jobs), inp.depot],
            selected_job_ids=[job.id for job in inp.jobs],
            objective_value=1.0,
        )

    monkeypatch.setattr(optimizer_module, "solve_vrpb", fake_solve)
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        linehaul.location = "Same place"
        backhaul.location = "Same place"
        plan = _make_plan(db, "demo-session", truck.id, [linehaul.id, backhaul.id])
        db.commit()
        asyncio.run(calculate(plan.id))

    inp = captured["input"]
    assert inp.travel_time_ms[(linehaul.id, backhaul.id)] == 0
    assert inp.travel_time_ms[(backhaul.id, linehaul.id)] == 0
    assert inp.travel_time_ms[("D", linehaul.id)] == inp.travel_time_ms[("D", backhaul.id)]


def test_optimizer_reserves_depot_namespace_when_job_id_is_d(monkeypatch):
    captured: dict[str, object] = {}

    def fake_solve(inp):
        captured["input"] = inp
        return VRPBResult(solution_status="FEASIBLE", termination_reason="COMPLETED", selected_job_ids=[job.id for job in inp.jobs])

    monkeypatch.setattr(optimizer_module, "solve_vrpb", fake_solve)
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        linehaul.id = "D"
        linehaul.location = "North"
        backhaul.location = "South"
        plan = _make_plan(db, "demo-session", truck.id, ["D", backhaul.id])
        db.commit()
        asyncio.run(calculate(plan.id))

    inp = captured["input"]
    assert inp.depot == optimizer_module.DEPOT_NODE
    assert inp.travel_time_ms[(inp.depot, "D")] > 0
    assert inp.travel_time_ms[(inp.depot, backhaul.id)] > 0


def test_start_trip_rejects_other_driver_and_stale_plan():
    with SessionLocal() as db:
        driver = db.scalar(select(User).where(User.username == "driver"))
        assert driver
        linehaul, backhaul, truck = _contract_seed_jobs(db, "demo-session", driver.id)
        plan = _make_plan(db, "demo-session", truck.id, [linehaul.id, backhaul.id])
        asyncio.run(calculate(plan.id))
        db.refresh(plan)
        session = db.get(SimulationSession, "demo-session")
        assert session
        session.state_version += 1
        db.commit()
        assert _command(db, session.id, driver.id, "START_TRIP", {"plan_id": plan.id, "truck_id": truck.id}).rejection_reason == "STALE_PLAN"


def test_invalid_queued_input_is_rejected_and_worker_processes_next_command():
    with SessionLocal() as db:
        session = db.get(SimulationSession, "demo-session")
        shipper = db.scalar(select(User).where(User.username == "shipper"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert session and shipper and admin
        session.owner_id = WORKER_ID
        session.worker_generation = 7
        bad = Command(id=str(uuid.uuid4()), session_id=session.id, user_id=shipper.id, idempotency_key="bad-input", type="CREATE_JOB", payload={"kind": "L", "location": "X", "pallets": "not-a-number", "weight_g": 1})
        bad_float = Command(id=str(uuid.uuid4()), session_id=session.id, user_id=admin.id, idempotency_key="bad-float", type="SET_SPEED", payload={"speed": float("nan")})
        good = Command(id=str(uuid.uuid4()), session_id=session.id, user_id=admin.id, idempotency_key="good-input", type="SET_SPEED", payload={"speed": 1})
        db.add_all([bad, bad_float, good])
        db.commit()

    process_commands("demo-session", 7)
    process_commands("demo-session", 7)
    process_commands("demo-session", 7)
    with SessionLocal() as db:
        assert db.get(Command, bad.id).status == "REJECTED"
        assert db.get(Command, bad.id).rejection_reason in {"INVALID_JOB", "INVALID_INPUT"}
        assert db.get(Command, bad_float.id).status == "REJECTED"
        assert db.get(Command, bad_float.id).rejection_reason == "INVALID_INPUT"
        assert db.get(Command, good.id).status == "APPLIED"
