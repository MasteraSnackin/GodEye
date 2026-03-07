from src.agents.tools import compute_noisy_or_confidence, derive_event_severity


def test_noisy_or_confidence_baseline():
    assert compute_noisy_or_confidence(0) == 0.0


def test_noisy_or_confidence_growth():
    assert compute_noisy_or_confidence(1) == 0.2
    assert compute_noisy_or_confidence(2) == 0.36
    assert compute_noisy_or_confidence(5) == 0.67


def test_derive_event_severity_by_confidence():
    assert derive_event_severity(0.9) == "high"
    assert derive_event_severity(0.5) == "medium"
    assert derive_event_severity(0.2) == "low"


def test_derive_event_severity_overrides_jamming():
    assert derive_event_severity(0.2, feed_type="jamming") == "high"
