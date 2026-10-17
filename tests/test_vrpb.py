from shared.vrpb import Job, VRPBInput, solve_vrpb


def test_vrpb_visits_all_linehaul_before_backhaul_and_respects_capacity():
    result = solve_vrpb(
        VRPBInput(
            depot="D",
            travel_time_ms={
                ("D", "L1"): 10,
                ("L1", "B1"): 10,
                ("B1", "D"): 10,
                ("D", "B1"): 40,
                ("B1", "L1"): 1,
                ("L1", "D"): 10,
            },
            jobs=[
                Job(id="linehaul", kind="L", location="L1", pallets=2, weight_g=200),
                Job(id="backhaul", kind="B", location="B1", pallets=1, weight_g=100, revenue_yen=500),
            ],
            capacity_pallets=3,
            capacity_weight_g=500,
        )
    )

    assert result.solution_status in {"OPTIMAL", "FEASIBLE"}
    assert result.route.index("L1") < result.route.index("B1")
    assert result.final_load_pallets == 1


def test_vrpb_rejects_infeasible_mandatory_linehaul():
    result = solve_vrpb(
        VRPBInput(
            depot="D",
            travel_time_ms={("D", "L1"): 10, ("L1", "D"): 10},
            jobs=[Job(id="linehaul", kind="L", location="L1", pallets=4, weight_g=1)],
            capacity_pallets=3,
            capacity_weight_g=10,
        )
    )

    assert result.solution_status == "INFEASIBLE"
    assert result.termination_reason == "COMPLETED"

