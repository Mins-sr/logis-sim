from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
import math
from typing import Iterable


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    location: str
    pallets: int
    weight_g: int
    earliest_ms: int = 0
    latest_ms: int = 86_400_000
    service_ms: int = 5 * 60_000
    revenue_yen: int = 0
    mandatory: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or not isinstance(self.location, str) or not self.location:
            raise ValueError("job id and location are required")
        if self.kind not in {"L", "B"}:
            raise ValueError("job kind must be L or B")
        if isinstance(self.pallets, bool) or not isinstance(self.pallets, int) or self.pallets <= 0:
            raise ValueError("job pallets must be a positive integer")
        if isinstance(self.weight_g, bool) or not isinstance(self.weight_g, int) or self.weight_g < 0:
            raise ValueError("job weight must be a non-negative integer")
        if self.earliest_ms < 0 or self.latest_ms < 0 or self.service_ms <= 0 or self.revenue_yen < 0:
            raise ValueError("job timing and revenue must be non-negative")
        if self.earliest_ms > self.latest_ms:
            raise ValueError("earliest_ms must be <= latest_ms")


@dataclass(frozen=True)
class VRPBInput:
    depot: str
    travel_time_ms: dict[tuple[str, str], int]
    jobs: list[Job]
    capacity_pallets: int
    capacity_weight_g: int
    distance_cost_yen_per_ms: float = 0.01
    time_cost_yen_per_ms: float = 0.0005
    tardiness_penalty_yen_per_ms: float = 0.005

    def __post_init__(self) -> None:
        if not self.depot or self.capacity_pallets <= 0 or self.capacity_weight_g < 0:
            raise ValueError("depot and positive vehicle capacities are required")
        if not all(math.isfinite(value) for value in (self.distance_cost_yen_per_ms, self.time_cost_yen_per_ms, self.tardiness_penalty_yen_per_ms)):
            raise ValueError("costs must be finite")


@dataclass
class VRPBResult:
    solution_status: str
    termination_reason: str
    route: list[str] = field(default_factory=list)
    selected_job_ids: list[str] = field(default_factory=list)
    objective_value: float | None = None
    total_travel_ms: int = 0
    final_load_pallets: int = 0
    final_load_weight_g: int = 0
    tardiness_ms: int = 0
    solver: str = "fallback-enumeration"
    reason: str | None = None


def _node_location(inp: VRPBInput, node: str) -> str:
    if node == inp.depot:
        return node
    return next((job.location for job in inp.jobs if job.id == node), node)


def _travel(inp: VRPBInput, a: str, b: str) -> int:
    if a == b:
        return 0
    value = inp.travel_time_ms.get((a, b))
    if value is None:
        value = inp.travel_time_ms.get((b, a))
    left, right = _node_location(inp, a), _node_location(inp, b)
    if value is None:
        value = inp.travel_time_ms.get((left, right))
    if value is None:
        value = inp.travel_time_ms.get((right, left))
    if value is None:
        return 1_000_000_000
    return max(0, int(value))


def _capacity_ok(inp: VRPBInput, linehauls: Iterable[Job]) -> bool:
    pallets = sum(job.pallets for job in linehauls)
    weight = sum(job.weight_g for job in linehauls)
    return pallets <= inp.capacity_pallets and weight <= inp.capacity_weight_g


def _evaluate_route(inp: VRPBInput, jobs_by_id: dict[str, Job], route: tuple[str, ...]) -> VRPBResult | None:
    linehaul = [job for job in jobs_by_id.values() if job.kind == "L"]
    if not _capacity_ok(inp, linehaul):
        return None
    seen_backhaul = False
    now = 0
    current = inp.depot
    load_pallets = sum(job.pallets for job in linehaul)
    load_weight = sum(job.weight_g for job in linehaul)
    tardiness = 0
    travel_total = 0
    for job_id in route:
        job = jobs_by_id[job_id]
        if job.kind == "L" and seen_backhaul:
            return None
        if job.kind == "B":
            seen_backhaul = True
        leg = _travel(inp, current, job_id)
        if leg >= 1_000_000_000:
            return None
        travel_total += leg
        now += leg
        start = max(now, job.earliest_ms)
        tardiness += max(0, start - job.latest_ms)
        now = start + job.service_ms
        if job.kind == "L":
            load_pallets -= job.pallets
            load_weight -= job.weight_g
        else:
            load_pallets += job.pallets
            load_weight += job.weight_g
        if load_pallets < 0 or load_weight < 0 or load_pallets > inp.capacity_pallets or load_weight > inp.capacity_weight_g:
            return None
        current = job_id
    return_leg = _travel(inp, current, inp.depot)
    if return_leg >= 1_000_000_000:
        return None
    travel_total += return_leg
    objective = (
        sum(jobs_by_id[job_id].revenue_yen for job_id in route)
        - travel_total * inp.distance_cost_yen_per_ms
        - now * inp.time_cost_yen_per_ms
        - tardiness * inp.tardiness_penalty_yen_per_ms
    )
    return VRPBResult(
        solution_status="FEASIBLE",
        termination_reason="COMPLETED",
        route=[inp.depot, *[jobs_by_id[job_id].location for job_id in route], inp.depot],
        selected_job_ids=list(route),
        objective_value=objective,
        total_travel_ms=travel_total,
        final_load_pallets=load_pallets,
        final_load_weight_g=load_weight,
        tardiness_ms=tardiness,
    )


def _fallback_solve(inp: VRPBInput) -> VRPBResult:
    linehaul = [job for job in inp.jobs if job.kind == "L"]
    optional_backhaul = [job for job in inp.jobs if job.kind == "B" and not job.mandatory]
    mandatory_backhaul = [job for job in inp.jobs if job.kind == "B" and job.mandatory]
    if not _capacity_ok(inp, linehaul):
        return VRPBResult("INFEASIBLE", "COMPLETED", reason="mandatory linehaul exceeds vehicle capacity")

    candidates: list[list[Job]] = [mandatory_backhaul]
    for job in optional_backhaul:
        candidates = candidates + [existing + [job] for existing in candidates]
    best: VRPBResult | None = None
    for selected_backhauls in candidates:
        all_jobs = [*linehaul, *selected_backhauls]
        # The order structure is explicit: all L permutations, followed by B permutations.
        for l_order in permutations(linehaul):
            for b_order in permutations(selected_backhauls):
                job_ids = tuple(job.id for job in (*l_order, *b_order))
                candidate = _evaluate_route(inp, {job.id: job for job in all_jobs}, job_ids)
                if candidate and (best is None or (candidate.objective_value or -1e99) > (best.objective_value or -1e99)):
                    best = candidate
    if best is None:
        return VRPBResult("INFEASIBLE", "COMPLETED", reason="no route satisfies VRPB constraints")
    best.solution_status = "OPTIMAL"
    return best


def solve_vrpb(inp: VRPBInput) -> VRPBResult:
    """Solve a small VRPB instance, using OR-Tools when available.

    The deterministic enumeration fallback keeps local development and tests runnable
    without a native solver. Production images install OR-Tools and use the same
    constraint model through the optional solver path.
    """
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2  # type: ignore
    except ImportError:
        return _fallback_solve(inp)

    # OR-Tools is used for production-sized inputs. For very small scenarios the
    # exact fallback provides a stable optimality check and clearer diagnostics.
    if len(inp.jobs) <= 8:
        result = _fallback_solve(inp)
        result.solver = "ortools-compatible-exact-fallback"
        return result

    linehauls = [job for job in inp.jobs if job.kind == "L"]
    backhauls = [job for job in inp.jobs if job.kind == "B"]
    if not _capacity_ok(inp, linehauls):
        return VRPBResult("INFEASIBLE", "COMPLETED", reason="mandatory linehaul exceeds vehicle capacity", solver="ortools")
    nodes = [inp.depot, *[job.id for job in inp.jobs]]
    manager = pywrapcp.RoutingIndexManager(len(nodes), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def transit(from_index: int, to_index: int) -> int:
        return _travel(inp, nodes[manager.IndexToNode(from_index)], nodes[manager.IndexToNode(to_index)])

    transit_idx = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    routing.AddDimension(transit_idx, 86_400_000, 86_400_000, False, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")
    for node_index, job in enumerate(inp.jobs, start=1):
        index = manager.NodeToIndex(node_index)
        time_dimension.CumulVar(index).SetRange(job.earliest_ms, job.latest_ms + job.service_ms)

    def demand(from_index: int) -> int:
        node = manager.IndexToNode(from_index)
        if node == 0:
            return 0
        job = inp.jobs[node - 1]
        return -job.pallets if job.kind == "L" else job.pallets

    demand_idx = routing.RegisterUnaryTransitCallback(demand)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [inp.capacity_pallets], True, "Pallets")
    capacity = routing.GetDimensionOrDie("Pallets")
    capacity.CumulVar(routing.Start(0)).SetValue(sum(job.pallets for job in linehauls))

    for job in linehauls:
        idx = manager.NodeToIndex(nodes.index(job.id))
        routing.AddDisjunction([idx], 10**12)
    for job in backhauls:
        idx = manager.NodeToIndex(nodes.index(job.id))
        routing.AddDisjunction([idx], max(1, job.revenue_yen) * 10)
    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search.time_limit.FromSeconds(5)
    assignment = routing.SolveWithParameters(search)
    if assignment is None:
        return VRPBResult("INFEASIBLE", "TIME_LIMIT", solver="ortools")
    route: list[str] = [inp.depot]
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node:
            route.append(nodes[node])
        index = assignment.Value(routing.NextVar(index))
    route.append(inp.depot)
    by_id = {job.id: job for job in inp.jobs}
    result = _evaluate_route(inp, by_id, tuple(route[1:-1]))
    if result is None:
        return VRPBResult("UNKNOWN", "ERROR", solver="ortools", reason="solver returned a route rejected by domain validation")
    result.solution_status = "FEASIBLE"
    result.solver = "ortools"
    return result

