from src.services.progress_policy import ProgressPolicy


def test_repeated_identical_work_requests_repair_without_global_round_cap():
    policy = ProgressPolicy(repeat_threshold=3)
    assert policy.observe(fingerprint="same", progressed=False).action == "continue"
    assert policy.observe(fingerprint="same", progressed=False).action == "continue"
    assert policy.observe(fingerprint="same", progressed=False).action == "repair"


def test_repeated_unknown_effects_stop():
    policy = ProgressPolicy(unknown_threshold=2)
    assert policy.observe(fingerprint="a", progressed=False, status="unknown").action == "continue"
    assert policy.observe(fingerprint="b", progressed=False, status="unknown").action == "stop"
