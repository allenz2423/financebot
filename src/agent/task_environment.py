"""Host-owned contract for disposable, no-network task environments.

This module deliberately defines an interface and an in-memory fake only. It
does not launch processes, containers, browsers, or any other runtime. Runtime
configuration is derived solely from fixed host constants; callers can select
only a named resource profile and allowlisted operation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from collections.abc import Callable, Iterable, Iterator
from types import MappingProxyType
from typing import Mapping, Protocol


class TaskEnvironmentError(RuntimeError):
    """Base error for task environment contract violations."""


class EnvironmentNotFound(TaskEnvironmentError):
    """The host does not recognize the supplied environment lease."""


class EnvironmentOwnershipError(PermissionError, TaskEnvironmentError):
    """The authenticated owner/task pair does not own this environment."""


class EnvironmentStateError(TaskEnvironmentError):
    """An operation is invalid for the environment's current lifecycle state."""


class ResourceProfile(str, Enum):
    """Fixed resource presets; these values are not guest-editable settings."""

    SMALL = "small"
    STANDARD = "standard"


@dataclass(frozen=True)
class ResourceLimits:
    cpu_millis: int
    memory_mb: int
    wall_seconds: int
    pids: int
    output_bytes: int
    network_enabled: bool = False
    credentials: tuple[str, ...] = ()


# Conservative host-owned profiles. Keep immutable and intentionally narrow.
RESOURCE_PROFILES: Mapping[ResourceProfile, ResourceLimits] = MappingProxyType({
    ResourceProfile.SMALL: ResourceLimits(500, 512, 60, 32, 16_384),
    ResourceProfile.STANDARD: ResourceLimits(1_000, 1_024, 300, 64, 65_536),
})


class EnvironmentOperation(str, Enum):
    SUBMIT_WORK = "submit_work"
    READ_OUTPUT = "read_output"
    CANCEL = "cancel"
    CLEANUP = "cleanup"


class EnvironmentState(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLEANED = "cleaned"


@dataclass(frozen=True)
class TaskIdentity:
    """Identity taken from trusted host task state, never from guest input."""

    owner_id: str
    task_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id is required")


@dataclass(frozen=True)
class TaskEnvironmentLease:
    """Host-issued capability binding one task to one fixed profile."""

    environment_id: str
    identity: TaskIdentity
    profile: ResourceProfile
    created_at: datetime
    expires_at: datetime


class TaskEnvironmentAdapter(Protocol):
    """Narrow host adapter boundary. Implementations must not accept config."""

    def provision(self, lease: TaskEnvironmentLease, limits: ResourceLimits) -> None: ...

    def perform(
        self,
        environment_id: str,
        operation: EnvironmentOperation,
        *,
        max_output_bytes: int,
    ) -> Iterable[bytes]: ...

    def cancel(self, environment_id: str) -> None: ...

    def cleanup(self, environment_id: str) -> None: ...

    def active_environments(self) -> tuple[str, ...]: ...


def bounded_output(data: bytes | bytearray | memoryview | str, limit: int) -> bytes:
    """Return at most limit UTF-8 bytes, truncating on a character boundary."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("output limit must be a non-negative integer")
    encoded = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(encoded) <= limit:
        return encoded
    truncated = encoded[:limit]
    return truncated.decode("utf-8", errors="ignore").encode("utf-8")


class TaskEnvironmentManager:
    """Validates host leases and applies lifecycle/output policy to an adapter."""

    def __init__(self, adapter: TaskEnvironmentAdapter) -> None:
        self._adapter = adapter
        self._leases: dict[str, TaskEnvironmentLease] = {}
        self._states: dict[str, EnvironmentState] = {}

    def create(
        self,
        identity: TaskIdentity,
        profile: ResourceProfile,
        *,
        now: datetime | None = None,
    ) -> TaskEnvironmentLease:
        if not isinstance(identity, TaskIdentity):
            raise TypeError("identity must come from the host TaskIdentity type")
        if not isinstance(profile, ResourceProfile):
            raise ValueError("profile must be a fixed ResourceProfile")
        instant = _utc(now or datetime.now(timezone.utc))
        limits = RESOURCE_PROFILES[profile]
        lease = TaskEnvironmentLease(
            environment_id=uuid.uuid4().hex,
            identity=identity,
            profile=profile,
            created_at=instant,
            expires_at=instant + timedelta(seconds=limits.wall_seconds),
        )
        self._adapter.provision(lease, limits)
        self._leases[lease.environment_id] = lease
        self._states[lease.environment_id] = EnvironmentState.ACTIVE
        return lease

    def perform(
        self,
        lease: TaskEnvironmentLease,
        identity: TaskIdentity,
        operation: EnvironmentOperation,
        *,
        now: datetime | None = None,
    ) -> bytes:
        record = self._authorize(lease, identity)
        if not isinstance(operation, EnvironmentOperation):
            raise ValueError("operation is not allowlisted")
        if operation in {EnvironmentOperation.CANCEL, EnvironmentOperation.CLEANUP}:
            raise ValueError("use the host lifecycle methods for cancel and cleanup")
        if self._states[record.environment_id] is not EnvironmentState.ACTIVE:
            raise EnvironmentStateError("environment is not active")
        if _utc(now or datetime.now(timezone.utc)) >= record.expires_at:
            self._states[record.environment_id] = EnvironmentState.TIMED_OUT
            self._cleanup_idempotent(record.environment_id)
            raise EnvironmentStateError("environment timed out and was cleaned up")
        output_limit = RESOURCE_PROFILES[record.profile].output_bytes
        # The real adapter must stop reading/streaming once this bound is
        # reached; truncation only after returning a full buffer is not a
        # resource bound. The manager validates the adapter's contract too.
        chunks = self._adapter.perform(
            record.environment_id, operation, max_output_bytes=output_limit
        )
        output = bytearray()
        iterator: Iterator[bytes] = iter(chunks)
        try:
            while len(output) < output_limit:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                remaining = output_limit - len(output)
                if not isinstance(chunk, bytes) or len(chunk) > remaining:
                    raise TaskEnvironmentError(
                        "adapter violated the bounded-output contract"
                    )
                output.extend(chunk)
            # Do not advance a streaming source past the cap just to discover
            # whether it has another chunk; close it at the exact boundary.
            close = getattr(iterator, "close", None)
            if len(output) >= output_limit and callable(close):
                close()
        except Exception:
            self._states[record.environment_id] = EnvironmentState.CANCELLED
            self._cleanup_idempotent(record.environment_id)
            raise
        return bytes(output)

    def cancel(self, lease: TaskEnvironmentLease, identity: TaskIdentity) -> None:
        record = self._authorize(lease, identity)
        if self._states[record.environment_id] is EnvironmentState.CLEANED:
            return
        if self._states[record.environment_id] is EnvironmentState.ACTIVE:
            try:
                self._adapter.cancel(record.environment_id)
            finally:
                self._states[record.environment_id] = EnvironmentState.CANCELLED
                self._cleanup_idempotent(record.environment_id)
        else:
            self._cleanup_idempotent(record.environment_id)

    def cleanup(self, lease: TaskEnvironmentLease, identity: TaskIdentity) -> None:
        record = self._authorize(lease, identity)
        self._cleanup_idempotent(record.environment_id)

    def expire(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Expire active host leases; intended for a trusted manager sweep."""
        instant = _utc(now or datetime.now(timezone.utc))
        expired: list[str] = []
        for environment_id, lease in tuple(self._leases.items()):
            if (
                self._states[environment_id]
                in {EnvironmentState.ACTIVE, EnvironmentState.TIMED_OUT}
                and instant >= lease.expires_at
            ):
                self._states[environment_id] = EnvironmentState.TIMED_OUT
                self._cleanup_idempotent(environment_id)
                expired.append(environment_id)
        return tuple(expired)

    def recover(self, live_tasks: frozenset[TaskIdentity] | set[TaskIdentity]) -> tuple[str, ...]:
        """Clean adapter environments not backed by an active host task record."""
        live = frozenset(live_tasks)
        removed: list[str] = []
        for environment_id in self._adapter.active_environments():
            lease = self._leases.get(environment_id)
            if lease is None or lease.identity not in live:
                self._adapter.cleanup(environment_id)
                self._states[environment_id] = EnvironmentState.CLEANED
                removed.append(environment_id)
        return tuple(removed)

    def state(self, lease: TaskEnvironmentLease, identity: TaskIdentity) -> EnvironmentState:
        record = self._authorize(lease, identity)
        return self._states[record.environment_id]

    def _authorize(
        self, lease: TaskEnvironmentLease, identity: TaskIdentity
    ) -> TaskEnvironmentLease:
        if not isinstance(identity, TaskIdentity):
            raise EnvironmentOwnershipError("trusted owner/task identity is required")
        stored = self._leases.get(getattr(lease, "environment_id", ""))
        if stored is None or stored != lease:
            raise EnvironmentNotFound("environment lease is not host-owned")
        if stored.identity != identity:
            raise EnvironmentOwnershipError("environment belongs to another owner or task")
        return stored

    def _cleanup_idempotent(self, environment_id: str) -> None:
        if self._states.get(environment_id) is EnvironmentState.CLEANED:
            return
        self._adapter.cleanup(environment_id)
        self._states[environment_id] = EnvironmentState.CLEANED


class FakeTaskEnvironmentAdapter:
    """Deterministic test adapter; models bookkeeping, never starts a runtime."""

    def __init__(
        self,
        output: bytes | str = b"",
        *,
        stream_factory: Callable[[], Iterable[bytes]] | None = None,
    ) -> None:
        self.output = output.encode("utf-8") if isinstance(output, str) else bytes(output)
        self.stream_factory = stream_factory
        self.records: dict[str, tuple[TaskEnvironmentLease, ResourceLimits]] = {}
        self.operations: list[tuple[str, EnvironmentOperation]] = []
        self.cancel_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.output_limits: list[tuple[str, int]] = []
        self.source_bytes_consumed: dict[str, int] = {}

    def provision(self, lease: TaskEnvironmentLease, limits: ResourceLimits) -> None:
        self.records[lease.environment_id] = (lease, limits)

    def perform(
        self,
        environment_id: str,
        operation: EnvironmentOperation,
        *,
        max_output_bytes: int,
    ) -> Iterable[bytes]:
        if environment_id not in self.records:
            raise EnvironmentNotFound(environment_id)
        self.operations.append((environment_id, operation))
        self.output_limits.append((environment_id, max_output_bytes))
        consumed = 0
        self.source_bytes_consumed[environment_id] = 0
        source: Iterable[bytes]
        if self.stream_factory is not None:
            source = self.stream_factory()
        else:
            source = (
                self.output[offset:offset + 4096]
                for offset in range(0, len(self.output), 4096)
            )
        for chunk in source:
            consumed += len(chunk)
            self.source_bytes_consumed[environment_id] = consumed
            yield chunk

    def cancel(self, environment_id: str) -> None:
        if environment_id in self.records:
            self.cancel_calls.append(environment_id)

    def cleanup(self, environment_id: str) -> None:
        if environment_id in self.records:
            self.cleanup_calls.append(environment_id)
            del self.records[environment_id]

    def active_environments(self) -> tuple[str, ...]:
        return tuple(self.records)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
