from src.agents.graph import compute_previous_window_bounds


def test_compute_previous_window_bounds():
    prev_from, prev_to = compute_previous_window_bounds("2026-03-07T10:00:00Z", "2026-03-07T11:00:00Z")
    assert prev_from == "2026-03-07T09:00:00Z"
    assert prev_to == "2026-03-07T10:00:00Z"


def test_compute_previous_window_bounds_invalid_order():
    try:
        compute_previous_window_bounds("2026-03-07T11:00:00Z", "2026-03-07T10:00:00Z")
    except ValueError as e:
        assert str(e) == "to_time must be later than from_time"
    else:
        raise AssertionError("Expected ValueError")
