import pytest

from shared.warehouse import WarehouseAdapter, WarehouseInvariantError


def test_agv_reservation_is_exclusive_and_snapshot_restores():
    warehouse = WarehouseAdapter(agv_ids=["agv-1"], staging_capacity=2)
    assert warehouse.book_agv("agv-1", "task-1") is True
    assert warehouse.book_agv("agv-1", "task-2") is False
    warehouse.inject_transport_order({"id": "task-1", "pallets": 1})
    warehouse.step_single_event()

    snapshot = warehouse.export_snapshot()
    restored = WarehouseAdapter(agv_ids=["agv-1"], staging_capacity=2)
    restored.restore_snapshot(snapshot)
    assert restored.export_snapshot() == snapshot
    restored.release_agv("agv-1")
    restored.assert_invariants()


def test_staging_capacity_is_enforced():
    warehouse = WarehouseAdapter(agv_ids=["agv-1"], staging_capacity=1)
    warehouse.inject_transport_order({"id": "task-1", "pallets": 1})
    with pytest.raises(WarehouseInvariantError):
        warehouse.inject_transport_order({"id": "task-2", "pallets": 1})

