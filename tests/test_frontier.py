from radio_db.services.frontier import ucb_priority


def test_ucb_priority_unseen_queries_preferred() -> None:
    assert ucb_priority(run_count=0, yield_new=0, total_runs=100) > ucb_priority(
        run_count=10, yield_new=1, total_runs=100
    )
