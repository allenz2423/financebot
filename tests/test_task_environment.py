from datetime import datetime, timedelta, timezone

import pytest

from src.agent.task_environment import (
    EnvironmentOperation,
    EnvironmentOwnershipError,
    EnvironmentState,
    EnvironmentStateError,
    FakeTaskEnvironmentAdapter,
    RESOURCE_PROFILES,
    ResourceProfile,
    TaskEnvironmentError,
    TaskEnvironmentManager,
    TaskIdentity,
    bounded_output,
)


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def setup_manager(output=b"ok"):
    adapter = FakeTaskEnvironmentAdapter(output)
    manager = TaskEnvironmentManager(adapter)
    owner_task = TaskIdentity("owner-1", "task-1")
    lease = manager.create(owner_task, ResourceProfile.SMALL, now=NOW)
    return adapter, manager, owner_task, lease


def test_cross_owner_and_cross_task_cannot_use_or_clean_lease():
    adapter, manager, identity, lease = setup_manager()

    with pytest.raises(EnvironmentOwnershipError):
        manager.perform(
            lease,
            TaskIdentity("owner-2", "task-1"),
            EnvironmentOperation.SUBMIT_WORK,
            now=NOW,
        )
    with pytest.raises(EnvironmentOwnershipError):
        manager.cleanup(lease, TaskIdentity(identity.owner_id, "task-2"))

    assert adapter.operations == []
    assert adapter.cleanup_calls == []
    assert lease.environment_id in adapter.records


@pytest.mark.parametrize("profile", ["small", "custom", ""])
def test_profile_must_be_a_fixed_host_enum(profile):
    manager = TaskEnvironmentManager(FakeTaskEnvironmentAdapter())
    with pytest.raises(ValueError):
        manager.create(TaskIdentity("owner", "task"), profile, now=NOW)


def test_guest_runtime_configuration_is_not_an_input_and_profiles_are_fixed():
    adapter, manager, identity, lease = setup_manager()

    with pytest.raises(TypeError):
        manager.create(identity, ResourceProfile.SMALL, runtime_config={"network": True})

    limits = adapter.records[lease.environment_id][1]
    assert limits == RESOURCE_PROFILES[ResourceProfile.SMALL]
    assert limits.network_enabled is False
    assert limits.credentials == ()
    with pytest.raises(TypeError):
        RESOURCE_PROFILES[ResourceProfile.SMALL] = limits


def test_unknown_operation_rejected_before_adapter_call():
    adapter, manager, identity, lease = setup_manager()

    with pytest.raises(ValueError, match="not allowlisted"):
        manager.perform(lease, identity, "shell", now=NOW)
    with pytest.raises(ValueError, match="lifecycle methods"):
        manager.perform(lease, identity, EnvironmentOperation.CLEANUP, now=NOW)

    assert adapter.operations == []


def test_output_is_capped_by_profile_and_utf8_is_not_split():
    reads = []

    def unbounded_lazy_source():
        for _ in range(100):
            reads.append("read")
            yield b"x" * 4096

    adapter = FakeTaskEnvironmentAdapter(stream_factory=unbounded_lazy_source)
    manager = TaskEnvironmentManager(adapter)
    identity = TaskIdentity("owner-1", "task-1")
    lease = manager.create(identity, ResourceProfile.SMALL, now=NOW)
    output = manager.perform(
        lease, identity, EnvironmentOperation.READ_OUTPUT, now=NOW
    )
    assert len(output) <= RESOURCE_PROFILES[ResourceProfile.SMALL].output_bytes
    assert len(output) == RESOURCE_PROFILES[ResourceProfile.SMALL].output_bytes
    assert len(reads) == 4
    assert len(reads) < 100
    assert adapter.output_limits == [(
        lease.environment_id,
        RESOURCE_PROFILES[ResourceProfile.SMALL].output_bytes,
    )]
    assert adapter.source_bytes_consumed[lease.environment_id] == len(output)
    assert adapter.source_bytes_consumed[lease.environment_id] <= adapter.output_limits[0][1]

    # Truncation never returns malformed UTF-8 when a multibyte character straddles
    # the byte boundary.
    assert bounded_output("a€z", 2) == b"a"
    assert bounded_output("a€z", 4) == "a€".encode()


def test_adapter_that_violates_output_bound_is_cleaned_and_rejected():
    class BrokenAdapter(FakeTaskEnvironmentAdapter):
        def perform(self, environment_id, operation, *, max_output_bytes):
            yield b"x" * (max_output_bytes + 1)

    adapter = BrokenAdapter()
    manager = TaskEnvironmentManager(adapter)
    identity = TaskIdentity("owner-1", "task-1")
    lease = manager.create(identity, ResourceProfile.SMALL, now=NOW)

    with pytest.raises(TaskEnvironmentError, match="bounded-output contract"):
        manager.perform(lease, identity, EnvironmentOperation.READ_OUTPUT, now=NOW)

    assert adapter.cleanup_calls == [lease.environment_id]
    assert manager.state(lease, identity) is EnvironmentState.CLEANED


def test_cancel_cleans_up_and_repeated_cancel_cleanup_are_idempotent():
    adapter, manager, identity, lease = setup_manager()

    manager.cancel(lease, identity)
    manager.cancel(lease, identity)
    manager.cleanup(lease, identity)
    manager.cleanup(lease, identity)

    assert adapter.cancel_calls == [lease.environment_id]
    assert adapter.cleanup_calls == [lease.environment_id]
    assert manager.state(lease, identity) is EnvironmentState.CLEANED
    assert lease.environment_id not in adapter.records


def test_timeout_sweep_cleans_up_and_expired_environment_cannot_run():
    adapter, manager, identity, lease = setup_manager()
    expiry = lease.expires_at

    assert manager.expire(now=expiry) == (lease.environment_id,)
    assert adapter.cleanup_calls == [lease.environment_id]
    with pytest.raises(EnvironmentStateError, match="not active"):
        manager.perform(lease, identity, EnvironmentOperation.SUBMIT_WORK, now=expiry)


def test_perform_after_deadline_cleans_before_returning_timeout():
    adapter, manager, identity, lease = setup_manager()

    with pytest.raises(EnvironmentStateError, match="timed out"):
        manager.perform(
            lease,
            identity,
            EnvironmentOperation.SUBMIT_WORK,
            now=lease.expires_at + timedelta(seconds=1),
        )

    assert adapter.operations == []
    assert adapter.cleanup_calls == [lease.environment_id]
    assert manager.state(lease, identity) is EnvironmentState.CLEANED


def test_recovery_cleans_orphaned_environments_but_keeps_live_task():
    adapter = FakeTaskEnvironmentAdapter()
    manager = TaskEnvironmentManager(adapter)
    live = TaskIdentity("owner-1", "live-task")
    stale = TaskIdentity("owner-1", "stale-task")
    live_lease = manager.create(live, ResourceProfile.SMALL, now=NOW)
    stale_lease = manager.create(stale, ResourceProfile.STANDARD, now=NOW)

    removed = manager.recover({live})

    assert removed == (stale_lease.environment_id,)
    assert live_lease.environment_id in adapter.records
    assert stale_lease.environment_id not in adapter.records
    assert adapter.cleanup_calls == [stale_lease.environment_id]


def test_recovery_after_manager_restart_cleans_unrecognized_host_records():
    adapter, manager, identity, lease = setup_manager()
    # The adapter's surviving record has no corresponding in-memory host lease
    # after restart, so recovery conservatively cleans it.
    restarted_manager = TaskEnvironmentManager(adapter)

    removed = restarted_manager.recover({identity})

    assert removed == (lease.environment_id,)
    assert adapter.cleanup_calls == [lease.environment_id]
    assert adapter.active_environments() == ()


def test_task_identity_requires_owner_and_task():
    with pytest.raises(ValueError, match="owner_id"):
        TaskIdentity("", "task")
    with pytest.raises(ValueError, match="task_id"):
        TaskIdentity("owner", " ")
