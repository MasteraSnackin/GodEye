import pytest
from api.replay.api import ReplayRequest
from pydantic import ValidationError


def test_replay_request_accepts_iso():
    req = ReplayRequest(
        from_time="2026-03-07T10:00:00Z",
        to_time="2026-03-07T11:00:00Z",
        scenario="EPIC_FURY_DEMO",
    )
    assert req.from_time == "2026-03-07T10:00:00Z"
    assert req.to_time == "2026-03-07T11:00:00Z"


def test_replay_request_rejects_non_iso():
    with pytest.raises(ValidationError):
        ReplayRequest(from_time="2026-13-99", to_time="2026-03-07T11:00:00Z")
