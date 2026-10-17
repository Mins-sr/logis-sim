from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db import SessionLocal, wait_for_database
from app.models import Job, Plan, SimulationSession, Truck, utcnow
from shared.vrpb import Job as VRPBJob
from shared.vrpb import VRPBInput, solve_vrpb


class OptimizationJobRequest(BaseModel):
    plan_id: str


app = FastAPI(title="SLAPStack Optimizer", version="1.2.0")
running_jobs: set[str] = set()


@app.on_event("startup")
def startup() -> None:
    wait_for_database()


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


def distance(a: str, b: str) -> int:
    if a == b:
        return 0
    # Deterministic local-road approximation used until a scenario road matrix is imported.
    seed = sum(ord(char) for char in a + b)
    return 600_000 + (seed % 7) * 120_000


async def calculate(plan_id: str) -> None:
    try:
        with SessionLocal() as db:
            plan = db.get(Plan, plan_id)
            if plan is None:
                return
            session = db.get(SimulationSession, plan.session_id)
            truck = db.scalar(select(Truck).where(Truck.session_id == plan.session_id).order_by(Truck.name))
            jobs = list(db.scalars(select(Job).where(Job.session_id == plan.session_id, Job.status.in_(["OPEN", "CONTRACTED", "IN_PROGRESS"]))))
            if session is None or truck is None:
                plan.solution_status = "INFEASIBLE"
                plan.termination_reason = "ERROR"
                plan.reason = "session or truck not found"
                plan.status = "REJECTED"
                db.commit()
                return
            locations = [job.location for job in jobs]
            travel: dict[tuple[str, str], int] = {}
            for left in ["D", *locations]:
                for right in ["D", *locations]:
                    travel[(left, right)] = distance(left, right)
            try:
                result = solve_vrpb(VRPBInput(
                    depot="D",
                    travel_time_ms=travel,
                    jobs=[VRPBJob(id=job.id, kind=job.kind, location=job.location, pallets=job.pallets, weight_g=job.weight_g, earliest_ms=job.earliest_ms, latest_ms=job.latest_ms, service_ms=job.service_ms, revenue_yen=job.revenue_yen, mandatory=job.status != "OPEN" or job.kind == "L") for job in jobs],
                    capacity_pallets=truck.capacity_pallets,
                    capacity_weight_g=truck.capacity_weight_g,
                ))
            except (TypeError, ValueError) as exc:
                # Invalid scenario data is a terminal plan result, not a task-level
                # exception. Persist it so clients never observe VALIDATING forever.
                plan.solution_status = "INFEASIBLE"
                plan.termination_reason = "ERROR"
                plan.reason = str(exc)
                plan.status = "REJECTED"
                db.commit()
                return
            plan.solution_status = result.solution_status
            plan.termination_reason = result.termination_reason
            plan.route = result.route
            plan.selected_job_ids = result.selected_job_ids
            plan.objective_value = result.objective_value
            plan.reason = result.reason
            if result.solution_status in {"OPTIMAL", "FEASIBLE"} and plan.base_state_version == session.state_version:
                plan.status = "APPLIED"
                plan.applied_at = utcnow()
            else:
                plan.status = "STALE" if result.solution_status in {"OPTIMAL", "FEASIBLE"} else "REJECTED"
            db.commit()
    finally:
        running_jobs.discard(plan_id)


@app.post("/optimization-jobs", status_code=202)
async def create_optimization_job(body: OptimizationJobRequest) -> dict[str, Any]:
    with SessionLocal() as db:
        plan = db.get(Plan, body.plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        if body.plan_id in running_jobs:
            return {"plan_id": body.plan_id, "status": "ACCEPTED", "deduplicated": True}
        running_jobs.add(body.plan_id)
    asyncio.create_task(calculate(body.plan_id))
    return {"plan_id": body.plan_id, "status": "ACCEPTED", "solution_status": "PENDING"}
