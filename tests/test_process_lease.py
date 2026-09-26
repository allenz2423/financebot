import pytest
import subprocess
import sys

from src.agent.process_lease import DatabaseProcessLease, ProcessLeaseUnavailable


def test_database_process_lease_rejects_a_second_lock_for_same_path(tmp_path):
    database_path = tmp_path / "tasks.sqlite3"
    first = DatabaseProcessLease(database_path)
    second = DatabaseProcessLease(database_path)

    first.acquire()
    try:
        with pytest.raises(ProcessLeaseUnavailable):
            second.acquire()
    finally:
        first.release()
        second.release()


def test_database_process_lease_can_be_reacquired_after_release(tmp_path):
    database_path = tmp_path / "tasks.sqlite3"
    first = DatabaseProcessLease(database_path)
    second = DatabaseProcessLease(database_path)

    first.acquire()
    first.release()

    second.acquire()
    second.release()


def test_database_process_lease_is_exclusive_across_processes(tmp_path):
    database_path = tmp_path / "tasks.sqlite3"
    lease = DatabaseProcessLease(database_path)
    lease.acquire()
    script = (
        "from src.agent.process_lease import DatabaseProcessLease, "
        "ProcessLeaseUnavailable; import sys; "
        "lease=DatabaseProcessLease(sys.argv[1]); "
        "\ntry: lease.acquire()\n"
        "except ProcessLeaseUnavailable: raise SystemExit(0)\n"
        "else: lease.release(); raise SystemExit(1)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(database_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    finally:
        lease.release()


def test_database_process_lease_rejects_in_memory_database():
    with pytest.raises(ValueError, match="file-backed database path"):
        DatabaseProcessLease(":memory:")
