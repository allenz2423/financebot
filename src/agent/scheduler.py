"""Capacity-aware model task scheduler and provider concurrency controller.

Implements Step 3 of docs/hermes-agentic-gap-review.md:
1. Hard per-provider client semaphore enforcing concurrency ceilings even for
   deliberately bypassing callers (defense in depth).
2. CapacityAwareScheduler coordinating interactive and background FIFO lanes per owner.
3. Fair round-robin owner rotation across ready owners.
4. Wait-time-based lane selection for continuous readiness with interactive tie-breaking.
5. Bounded unit execution and cooperative non-preemptive cancellation at dispatch boundaries.
6. Observable queue wait, inference time, tool time, and cancellations.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
from dataclasses import dataclass, field
import os
import threading
import time
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

_HELD_PROVIDER_SLOTS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "_HELD_PROVIDER_SLOTS", default=frozenset()
)


class ProviderLimiter:
    """Bounded semaphore at the shared provider-client/request boundary.

    Enforces the hard per-provider concurrency ceiling even if an overlooked
    or deliberately bypassing caller skips scheduler ordering.
    """

    def __init__(self, name: str, max_concurrency: int = 1) -> None:
        self.name = (name or "ollama").strip().lower()
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_count = 0
        self._waiting_count = 0

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def waiting_count(self) -> int:
        return self._waiting_count

    def acquire(self) -> _ProviderSlotContext:
        """Context manager to acquire provider concurrency slot."""
        return _ProviderSlotContext(self)


class _ProviderSlotContext:
    def __init__(self, limiter: ProviderLimiter) -> None:
        self.limiter = limiter
        self._already_held = False
        self._reset_token: contextvars.Token[frozenset[str]] | None = None

    async def __aenter__(self) -> _ProviderSlotContext:
        held = _HELD_PROVIDER_SLOTS.get()
        if self.limiter.name in held:
            self._already_held = True
            return self

        self.limiter._waiting_count += 1
        try:
            await self.limiter._semaphore.acquire()
        finally:
            self.limiter._waiting_count -= 1

        self.limiter._active_count += 1
        self._reset_token = _HELD_PROVIDER_SLOTS.set(held | {self.limiter.name})
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._already_held:
            return

        if self._reset_token is not None:
            _HELD_PROVIDER_SLOTS.reset(self._reset_token)

        self.limiter._active_count -= 1
        self.limiter._semaphore.release()


class ProviderCapacityController:
    """Registry of per-provider limiters ensuring shared concurrency caps."""

    _instance: ProviderCapacityController | None = None
    _lock = threading.Lock()

    def __init__(self, default_concurrency: int = 1) -> None:
        self.default_concurrency = default_concurrency
        self._limiters: dict[str, ProviderLimiter] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> ProviderCapacityController:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._lock:
            cls._instance = None

    def get_limiter(
        self, provider: str | None = None, max_concurrency: int | None = None
    ) -> ProviderLimiter:
        key = (provider or "ollama").strip().lower()
        with self._registry_lock:
            if key not in self._limiters:
                concurrency = (
                    max_concurrency
                    if max_concurrency is not None
                    else int(os.getenv(f"{key.upper()}_CONCURRENCY", str(self.default_concurrency)))
                )
                self._limiters[key] = ProviderLimiter(key, max_concurrency=concurrency)
            return self._limiters[key]


def get_provider_limiter(
    provider: str | None = None, max_concurrency: int | None = None
) -> ProviderLimiter:
    return ProviderCapacityController.get_instance().get_limiter(provider, max_concurrency)


def provider_capacity(
    provider: str | None = None, max_concurrency: int | None = None
) -> _ProviderSlotContext:
    return get_provider_limiter(provider, max_concurrency).acquire()


def _make_default_future() -> asyncio.Future:
    try:
        return asyncio.get_running_loop().create_future()
    except RuntimeError:
        return None  # type: ignore[return-value]


@dataclass
class WorkUnit:
    """A bounded unit of execution (inference, tool batch/step, or turn)."""

    task_id: str
    owner_id: str
    lane: Literal["interactive", "background"]
    fn: Callable[[], Awaitable[Any]]
    unit_type: Literal["inference", "tool_step", "turn"] = "inference"
    provider: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)
    cancellation_requested: bool = False
    context: contextvars.Context = field(default_factory=contextvars.copy_context)
    metadata: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future = field(default_factory=_make_default_future)


@dataclass
class SchedulerMetrics:
    """Observability metrics for scheduler and dispatch operations."""

    queue_wait_times: collections.deque[float] = field(
        default_factory=lambda: collections.deque(maxlen=1000)
    )
    inference_times: collections.deque[float] = field(
        default_factory=lambda: collections.deque(maxlen=1000)
    )
    tool_times: collections.deque[float] = field(
        default_factory=lambda: collections.deque(maxlen=1000)
    )
    cancellations_count: int = 0
    dispatches_by_lane: dict[str, int] = field(
        default_factory=lambda: {"interactive": 0, "background": 0}
    )
    dispatches_by_owner: dict[str, int] = field(default_factory=dict)
    dispatch_history: collections.deque[dict[str, Any]] = field(
        default_factory=lambda: collections.deque(maxlen=1000)
    )

    def record_queue_wait(self, duration: float) -> None:
        self.queue_wait_times.append(duration)

    def record_inference(self, duration: float) -> None:
        self.inference_times.append(duration)

    def record_tool(self, duration: float) -> None:
        self.tool_times.append(duration)

    def record_cancellation(self) -> None:
        self.cancellations_count += 1

    def record_dispatch(
        self, owner_id: str, lane: str, task_id: str, unit_type: str, queue_wait: float
    ) -> None:
        self.dispatches_by_lane[lane] = self.dispatches_by_lane.get(lane, 0) + 1
        self.dispatches_by_owner[owner_id] = self.dispatches_by_owner.get(owner_id, 0) + 1
        self.dispatch_history.append({
            "owner_id": owner_id,
            "lane": lane,
            "task_id": task_id,
            "unit_type": unit_type,
            "queue_wait": queue_wait,
            "timestamp": time.monotonic(),
        })


class OwnerQueue:
    """FIFO lanes and dispatch tracking for a single owner."""

    def __init__(self, owner_id: str, max_queue_size: int = 8) -> None:
        self.owner_id = owner_id
        self.max_queue_size = max_queue_size
        self.interactive_lane: collections.deque[WorkUnit] = collections.deque()
        self.background_lane: collections.deque[WorkUnit] = collections.deque()
        # Initialized to -inf so that an un-dispatched lane ties with another un-dispatched lane
        self.last_dispatched_at: dict[str, float] = {
            "interactive": -float("inf"),
            "background": -float("inf"),
        }
        self.active_task_count: int = 0

    @property
    def total_queued(self) -> int:
        return len(self.interactive_lane) + len(self.background_lane)

    def has_ready_work(self) -> bool:
        return len(self.interactive_lane) > 0 or len(self.background_lane) > 0


class CapacityAwareScheduler:
    """Coordinates model inference and tool work across owners and lanes.

    Guarantees:
    - Round-robin owner rotation across ready owners.
    - Longest-waiting lane selected for continuously ready owners, with
      interactive winning wait-time ties.
    - Hard capacity ceiling enforced by ProviderLimiter.
    - Cooperative cancellation at dispatch boundaries (non-preemptive).
    - Observable queue wait, inference time, tool time, and cancellations.
    """

    def __init__(
        self,
        *,
        provider_name: str = "ollama",
        max_workers: int = 1,
        max_active_tasks_per_owner: int = 1,
        max_queue_per_owner: int = 8,
    ) -> None:
        self.provider_name = provider_name
        self.max_workers = max(1, int(max_workers))
        self.max_active_tasks_per_owner = max(1, int(max_active_tasks_per_owner))
        self.max_queue_per_owner = max(1, int(max_queue_per_owner))
        # Worker count is an upper bound; provider-specific operator limits
        # remain authoritative even when the scheduler has more workers.
        self.limiter = get_provider_limiter(self.provider_name)
        self.metrics = SchedulerMetrics()

        self._owners: dict[str, OwnerQueue] = {}
        self._owner_rotation: collections.deque[str] = collections.deque()
        self._active_workers: int = 0
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._running = False
        self._worker_tasks: list[asyncio.Task] = []
        # Child orchestration is not itself a worker unit because each child
        # must release capacity while awaiting its scheduled inference. This
        # separate admission semaphore bounds whole child turns (including
        # unscheduled tool execution) to the configured worker ceiling.
        child_limit = min(self.max_workers, self.limiter.max_concurrency)
        self._child_execution_semaphore = asyncio.Semaphore(child_limit)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_owner_queue(self, owner_id: str) -> OwnerQueue:
        if owner_id not in self._owners:
            self._owners[owner_id] = OwnerQueue(owner_id, self.max_queue_per_owner)
        return self._owners[owner_id]

    @contextlib.asynccontextmanager
    async def child_execution_slot(self):
        """Bound active child turns without occupying a scheduler worker."""
        await self._child_execution_semaphore.acquire()
        try:
            yield
        finally:
            self._child_execution_semaphore.release()

    async def submit(
        self,
        task_id: str,
        owner_id: str,
        lane: Literal["interactive", "background"],
        fn: Callable[[], Awaitable[Any]],
        *,
        unit_type: Literal["inference", "tool_step", "turn"] = "inference",
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> asyncio.Future:
        """Submit a work unit into the owner's FIFO lane."""
        async with self._condition:
            owner_queue = self.get_owner_queue(owner_id)
            target_lane = (
                owner_queue.interactive_lane
                if lane == "interactive"
                else owner_queue.background_lane
            )
            if len(target_lane) >= self.max_queue_per_owner:
                raise BufferError(
                    f"Queue limit ({self.max_queue_per_owner}) exceeded for owner {owner_id} in {lane} lane"
                )

            effective_provider = provider
            if effective_provider is None and unit_type == "inference":
                effective_provider = self.provider_name

            unit = WorkUnit(
                task_id=task_id,
                owner_id=owner_id,
                lane=lane,
                fn=fn,
                unit_type=unit_type,
                provider=effective_provider,
                enqueued_at=time.monotonic(),
                context=contextvars.copy_context(),
                metadata=metadata or {},
            )

            if unit.future is None:
                unit.future = asyncio.get_running_loop().create_future()

            if lane == "interactive":
                owner_queue.interactive_lane.append(unit)
            else:
                owner_queue.background_lane.append(unit)

            if (
                owner_id not in self._owner_rotation
                and owner_queue.active_task_count < self.max_active_tasks_per_owner
            ):
                self._owner_rotation.append(owner_id)

            self._condition.notify_all()
            return unit.future

    def request_cancellation(self, task_id: str) -> bool:
        """Request cooperative cancellation for a task at the dispatch boundary."""
        found = False
        for owner_queue in self._owners.values():
            for unit in list(owner_queue.interactive_lane) + list(owner_queue.background_lane):
                if unit.task_id == task_id:
                    unit.cancellation_requested = True
                    found = True
        return found

    def cancel_owner_work(self, owner_id: str) -> int:
        """Cancel all pending units in queue for the specified owner."""
        cancelled = 0
        owner_queue = self._owners.get(owner_id)
        if owner_queue:
            for unit in list(owner_queue.interactive_lane) + list(owner_queue.background_lane):
                unit.cancellation_requested = True
                cancelled += 1
        return cancelled

    def _has_ready_work_unlocked(self) -> bool:
        """Check if any owner in rotation has ready, dispatchable work and capacity is available."""
        if self._active_workers >= self.max_workers:
            return False
        for owner_id in self._owner_rotation:
            owner_queue = self._owners.get(owner_id)
            if (
                owner_queue is not None
                and owner_queue.has_ready_work()
                and owner_queue.active_task_count < self.max_active_tasks_per_owner
            ):
                return True
        return False

    def _select_next_unit(self) -> tuple[WorkUnit, OwnerQueue] | None:
        """Select the next work unit per fair owner rotation and lane rules.

        Must be called with self._lock / self._condition held.
        """
        if self._active_workers >= self.max_workers:
            return None

        now = time.monotonic()
        total_owners = len(self._owner_rotation)
        for _ in range(total_owners):
            owner_id = self._owner_rotation.popleft()
            owner_queue = self._owners.get(owner_id)
            if owner_queue is None or not owner_queue.has_ready_work():
                continue

            # Enforce max active tasks per owner (default: 1)
            if owner_queue.active_task_count >= self.max_active_tasks_per_owner:
                # Do NOT re-enqueue busy owner now. When their active task completes,
                # they will re-enter at the back of rotation.
                continue

            has_i = len(owner_queue.interactive_lane) > 0
            has_b = len(owner_queue.background_lane) > 0

            selected_lane: Literal["interactive", "background"]
            if has_i and has_b:
                # Both lanes ready: compare time waited since previous dispatch.
                # If neither has dispatched, both are inf (tie -> interactive wins).
                wait_i = now - owner_queue.last_dispatched_at["interactive"]
                wait_b = now - owner_queue.last_dispatched_at["background"]
                # Interactive wins ties (wait_i >= wait_b)
                if wait_i >= wait_b:
                    selected_lane = "interactive"
                else:
                    selected_lane = "background"
            elif has_i:
                selected_lane = "interactive"
            elif has_b:
                selected_lane = "background"
            else:
                continue

            target_queue = (
                owner_queue.interactive_lane
                if selected_lane == "interactive"
                else owner_queue.background_lane
            )
            unit: WorkUnit | None = None
            while target_queue:
                candidate = target_queue.popleft()
                if candidate.cancellation_requested or (
                    candidate.future is not None and candidate.future.cancelled()
                ):
                    self.metrics.record_cancellation()
                    if candidate.future is not None and not candidate.future.done():
                        candidate.future.cancel()
                    continue
                unit = candidate
                break

            if unit is None:
                # All units in this lane were cancelled; if owner still has work, re-enqueue
                if owner_queue.has_ready_work() and owner_id not in self._owner_rotation:
                    self._owner_rotation.append(owner_id)
                continue

            # Record dispatch state
            owner_queue.last_dispatched_at[selected_lane] = now
            owner_queue.active_task_count += 1
            self._active_workers += 1

            # Only re-enqueue owner if they can still dispatch more active tasks concurrently
            if (
                owner_queue.active_task_count < self.max_active_tasks_per_owner
                and owner_queue.has_ready_work()
            ):
                self._owner_rotation.append(owner_id)

            return unit, owner_queue

        return None

    async def dispatch_once(self) -> bool:
        """Execute one bounded unit (useful for deterministic tests & worker)."""
        async with self._condition:
            selected = self._select_next_unit()

        if selected is None:
            return False

        unit, owner_queue = selected
        try:
            queue_wait = time.monotonic() - unit.enqueued_at
            self.metrics.record_queue_wait(queue_wait)
            self.metrics.record_dispatch(
                unit.owner_id, unit.lane, unit.task_id, unit.unit_type, queue_wait
            )

            # Non-preemptive execution under provider semaphore when provider is specified.
            # Running inside unit.context via asyncio.create_task propagates contextvars across worker tasks.
            start_time = time.monotonic()
            try:
                async def _run_work() -> Any:
                    limiter = get_provider_limiter(unit.provider) if unit.provider else None
                    if limiter:
                        async with limiter.acquire():
                            res = unit.fn()
                            if asyncio.iscoroutine(res):
                                return await res
                            return res
                    else:
                        res = unit.fn()
                        if asyncio.iscoroutine(res):
                            return await res
                        return res

                work_task = asyncio.create_task(_run_work(), context=unit.context)
                try:
                    result = await work_task
                except asyncio.CancelledError:
                    work_task.cancel()
                    try:
                        await work_task
                    except asyncio.CancelledError:
                        pass
                    raise

                duration = time.monotonic() - start_time
                if unit.unit_type == "inference":
                    self.metrics.record_inference(duration)
                elif unit.unit_type == "tool_step":
                    self.metrics.record_tool(duration)

                if not unit.future.done():
                    unit.future.set_result(result)
            except asyncio.CancelledError:
                self.metrics.record_cancellation()
                if not unit.future.done():
                    unit.future.cancel()
                raise
            except Exception as exc:
                duration = time.monotonic() - start_time
                if unit.unit_type == "inference":
                    self.metrics.record_inference(duration)
                elif unit.unit_type == "tool_step":
                    self.metrics.record_tool(duration)
                if not unit.future.done():
                    unit.future.set_exception(exc)
        finally:
            async with self._condition:
                self._active_workers -= 1
                owner_queue.active_task_count -= 1
                if owner_queue.has_ready_work() and unit.owner_id not in self._owner_rotation:
                    self._owner_rotation.append(unit.owner_id)
                self._condition.notify_all()

        return True

    async def _worker_loop(self) -> None:
        """Background worker loop continuously processing units with condition signaling."""
        while self._running:
            async with self._condition:
                while self._running and not self._has_ready_work_unlocked():
                    await self._condition.wait()
                if not self._running:
                    break

            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f" [SCHEDULER] worker error: {type(exc).__name__}: {exc}")

    async def start(self) -> None:
        """Start the background scheduler worker(s)."""
        if self._running:
            return
        self._running = True
        for i in range(self.max_workers):
            task = asyncio.create_task(self._worker_loop(), name=f"scheduler_worker_{i}")
            self._worker_tasks.append(task)

    async def stop(self) -> None:
        """Stop the background scheduler cleanly."""
        self._running = False
        async with self._condition:
            self._condition.notify_all()
        for task in self._worker_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_tasks.clear()

    def get_metrics(self) -> SchedulerMetrics:
        return self.metrics


_GLOBAL_SCHEDULER: CapacityAwareScheduler | None = None
_GLOBAL_SCHEDULER_LOCK = threading.Lock()


def get_global_scheduler() -> CapacityAwareScheduler:
    global _GLOBAL_SCHEDULER
    with _GLOBAL_SCHEDULER_LOCK:
        if _GLOBAL_SCHEDULER is None:
            _GLOBAL_SCHEDULER = CapacityAwareScheduler()
        return _GLOBAL_SCHEDULER


def set_global_scheduler(scheduler: CapacityAwareScheduler | None) -> None:
    global _GLOBAL_SCHEDULER
    with _GLOBAL_SCHEDULER_LOCK:
        _GLOBAL_SCHEDULER = scheduler


def reset_global_scheduler() -> None:
    global _GLOBAL_SCHEDULER
    with _GLOBAL_SCHEDULER_LOCK:
        _GLOBAL_SCHEDULER = None


async def schedule_work(
    task_id: str,
    owner_id: str,
    lane: Literal["interactive", "background"],
    fn: Callable[[], Awaitable[Any]],
    *,
    unit_type: Literal["inference", "tool_step", "turn"] = "inference",
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Route work through the global capacity-aware scheduler."""
    scheduler = get_global_scheduler()
    future = await scheduler.submit(
        task_id, owner_id, lane, fn, unit_type=unit_type, provider=provider, metadata=metadata
    )
    try:
        # If scheduler background workers are not started, dispatch until completion
        if not scheduler.is_running:
            while not future.done():
                dispatched = await scheduler.dispatch_once()
                if not dispatched and not future.done():
                    async with scheduler._condition:
                        if not future.done():
                            try:
                                await asyncio.wait_for(scheduler._condition.wait(), timeout=0.05)
                            except asyncio.TimeoutError:
                                pass
        return await future
    except asyncio.CancelledError:
        scheduler.request_cancellation(task_id)
        if not future.done():
            future.cancel()
        raise


__all__ = [
    "CapacityAwareScheduler",
    "OwnerQueue",
    "ProviderCapacityController",
    "ProviderLimiter",
    "SchedulerMetrics",
    "WorkUnit",
    "get_global_scheduler",
    "get_provider_limiter",
    "provider_capacity",
    "reset_global_scheduler",
    "schedule_work",
    "set_global_scheduler",
]
