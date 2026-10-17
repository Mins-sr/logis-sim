from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WarehouseInvariantError(ValueError):
    pass


@dataclass
class WarehouseAdapter:
    agv_ids: list[str]
    staging_capacity: int
    engine_build_id: str = "local-adapter-1"
    sim_time_ms: int = 0
    event_seq: int = 0
    agv_reservations: dict[str, str] = field(default_factory=dict)
    staging: dict[str, int] = field(default_factory=dict)
    pending_orders: list[dict[str, Any]] = field(default_factory=list)

    def book_agv(self, agv_id: str, task_id: str) -> bool:
        if agv_id not in self.agv_ids:
            raise WarehouseInvariantError(f"unknown agv: {agv_id}")
        if agv_id in self.agv_reservations:
            return False
        self.agv_reservations[agv_id] = task_id
        self.assert_invariants()
        return True

    def release_agv(self, agv_id: str) -> None:
        self.agv_reservations.pop(agv_id, None)
        self.assert_invariants()

    def inject_transport_order(self, order: dict[str, Any]) -> None:
        pallets = int(order.get("pallets", 0))
        if pallets <= 0:
            raise WarehouseInvariantError("transport order must contain pallets")
        if self.staging_load() + pallets > self.staging_capacity:
            raise WarehouseInvariantError("staging area capacity exceeded")
        self.pending_orders.append(dict(order))
        self.staging[order["id"]] = pallets
        self.assert_invariants()

    def step_single_event(self) -> dict[str, Any] | None:
        if not self.pending_orders:
            return None
        order = self.pending_orders.pop(0)
        self.event_seq += 1
        self.sim_time_ms += int(order.get("duration_ms", 1_000))
        return {"event_seq": self.event_seq, "sim_time_ms": self.sim_time_ms, "type": "TRANSPORT_COMPLETED", "order_id": order["id"]}

    def staging_load(self) -> int:
        return sum(self.staging.values())

    def export_snapshot(self) -> dict[str, Any]:
        return {
            "snapshot_format_version": 1,
            "engine_build_id": self.engine_build_id,
            "sim_time_ms": self.sim_time_ms,
            "event_seq": self.event_seq,
            "agv_reservations": dict(sorted(self.agv_reservations.items())),
            "staging": dict(sorted(self.staging.items())),
            "pending_orders": [dict(order) for order in self.pending_orders],
        }

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("snapshot_format_version") != 1:
            raise WarehouseInvariantError("unsupported snapshot format")
        if snapshot.get("engine_build_id") != self.engine_build_id:
            raise WarehouseInvariantError("snapshot engine_build_id mismatch")
        self.sim_time_ms = int(snapshot["sim_time_ms"])
        self.event_seq = int(snapshot["event_seq"])
        self.agv_reservations = dict(snapshot["agv_reservations"])
        self.staging = dict(snapshot["staging"])
        self.pending_orders = [dict(order) for order in snapshot["pending_orders"]]
        self.assert_invariants()

    def assert_invariants(self) -> None:
        if self.staging_load() > self.staging_capacity:
            raise WarehouseInvariantError("staging area over capacity")
        if len(self.agv_reservations) != len(set(self.agv_reservations)):
            raise WarehouseInvariantError("duplicate AGV reservation")
        if any(agv not in self.agv_ids for agv in self.agv_reservations):
            raise WarehouseInvariantError("reservation references unknown AGV")
        if self.event_seq < 0 or self.sim_time_ms < 0:
            raise WarehouseInvariantError("simulation counters cannot be negative")

