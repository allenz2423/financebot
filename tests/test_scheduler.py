"""Unit and acceptance tests for Step 3: Capacity-Aware Scheduler and Provider Limiter.

Verifies all criteria from docs/hermes-agentic-gap-review.md (Step 3 Acceptance Gate):
1. Interactive and background paths use the same capacity controller when they share a provider.
2. Owner dispatch rotates instead of draining one owner's backlog.
3. For one owner with both lanes continuously ready, both receive dispatches and interactive wins a wait-time tie.
4. Every known caller is scheduler-routed, while a deliberately bypassing caller still cannot exceed the provider-client semaphore.
5. A queued turn runs at the next eligible boundary, with no preemption or latency guarantee claimed.
6. Queue wait, inference time, tool time, and cancellations are observable.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import pytest

from src.agent.scheduler import (
    CapacityAwareScheduler,
    ProviderCapacityController,
    ProviderLimiter,
    get_provider_limiter,
    provider_capacity,
    reset_global_scheduler,
    schedule_work,
)


@pytest.fixture(autouse=True)
def reset_provider_capacity():
    """Reset the global provider capacity registry and scheduler between tests."""
    ProviderCapacityController.reset_instance()
    reset_global_scheduler()
    yield
    ProviderCapacityController.reset_instance()
    reset_global_scheduler()


@pytest.mark.asyncio
async def test_provider_limiter_enforces_hard_concurrency_ceiling():
    """Verify that ProviderLimiter enforces max_concurrency even under heavy contention."""
    limiter = ProviderLimiter("ollama", max_concurrency=1)
    observed_concurrency = []
    max_observed = 0

    async def worker(worker_id: int):
        nonlocal max_observed
        async with limiter.acquire():
            current = limiter.active_count
            observed_concurrency.append(current)
            if current > max_observed:
                max_observed = current
            await asyncio.sleep(0.02)

    # 5 concurrent workers attempting to acquire the semaphore
    await asyncio.gather(*(worker(i) for i in range(5)))

    assert max_observed == 1
    assert all(c == 1 for c in observed_concurrency)
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_owner_dispatch_rotates_instead_of_draining_backlog():
    """Criterion 2: Owner dispatch rotates instead of draining one owner's backlog."""
    scheduler = CapacityAwareScheduler(max_workers=1, max_active_tasks_per_owner=1)
    dispatch_order = []

    def make_job(owner: str, task_name: str):
        async def job():
            dispatch_order.append(f"{owner}:{task_name}")
            return task_name
        return job

    # Owner A queues 3 jobs
    await scheduler.submit("t_a1", "owner_a", "interactive", make_job("owner_a", "1"))
    await scheduler.submit("t_a2", "owner_a", "interactive", make_job("owner_a", "2"))
    await scheduler.submit("t_a3", "owner_a", "interactive", make_job("owner_a", "3"))

    # Owner B queues 3 jobs
    await scheduler.submit("t_b1", "owner_b", "interactive", make_job("owner_b", "1"))
    await scheduler.submit("t_b2", "owner_b", "interactive", make_job("owner_b", "2"))
    await scheduler.submit("t_b3", "owner_b", "interactive", make_job("owner_b", "3"))

    # Dispatch 6 times
    for _ in range(6):
        dispatched = await scheduler.dispatch_once()
        assert dispatched is True

    # Expected round-robin rotation: A1, B1, A2, B2, A3, B3
    # NOT draining A's entire backlog (A1, A2, A3, B1, B2, B3)
    assert dispatch_order == [
        "owner_a:1",
        "owner_b:1",
        "owner_a:2",
        "owner_b:2",
        "owner_a:3",
        "owner_b:3",
    ]


@pytest.mark.asyncio
async def test_continuous_readiness_both_lanes_receive_dispatches_and_interactive_wins_tie():
    """Criterion 3: One owner with both lanes continuously ready:

    - Both receive dispatches.
    - Interactive wins a wait-time tie.
    """
    scheduler = CapacityAwareScheduler(max_workers=1, max_active_tasks_per_owner=1)
    dispatch_history = []

    def make_job(lane: str, idx: int):
        async def job():
            dispatch_history.append((lane, idx))
            return idx
        return job

    # Queue multiple items in both interactive and background lanes
    await scheduler.submit("i1", "owner_1", "interactive", make_job("interactive", 1))
    await scheduler.submit("i2", "owner_1", "interactive", make_job("interactive", 2))
    await scheduler.submit("i3", "owner_1", "interactive", make_job("interactive", 3))

    await scheduler.submit("b1", "owner_1", "background", make_job("background", 1))
    await scheduler.submit("b2", "owner_1", "background", make_job("background", 2))
    await scheduler.submit("b3", "owner_1", "background", make_job("background", 3))

    # Dispatch 6 times
    for _ in range(6):
        await scheduler.dispatch_once()

    # Step 1: Initial tie-breaking (neither lane dispatched yet). Interactive wins!
    assert dispatch_history[0] == ("interactive", 1)

    # Step 2: Background has waited since -inf, while interactive dispatched at step 1.
    # Background must be chosen.
    assert dispatch_history[1] == ("background", 1)

    # Both lanes receive fair alternating dispatches
    assert dispatch_history == [
        ("interactive", 1),
        ("background", 1),
        ("interactive", 2),
        ("background", 2),
        ("interactive", 3),
        ("background", 3),
    ]


@pytest.mark.asyncio
async def test_deliberate_bypassing_caller_cannot_exceed_provider_semaphore():
    """Criterion 4: Every known caller is scheduler-routed, while a deliberately bypassing

    caller still cannot exceed the provider-client semaphore.
    """
    scheduler = CapacityAwareScheduler(max_workers=1, provider_name="ollama")
    concurrency_log = []
    max_concurrency = 0

    async def scheduled_unit():
        nonlocal max_concurrency
        # Holds provider slot via scheduler
        current = scheduler.limiter.active_count
        concurrency_log.append(("scheduled", current))
        if current > max_concurrency:
            max_concurrency = current
        await asyncio.sleep(0.03)

    # Submit scheduled work
    fut = await scheduler.submit("t1", "owner", "interactive", scheduled_unit)

    # Deliberate bypassing caller directly calls provider_capacity without going through scheduler
    async def bypassing_caller():
        nonlocal max_concurrency
        # Bypasses scheduler entirely!
        async with provider_capacity("ollama"):
            current = scheduler.limiter.active_count
            concurrency_log.append(("bypassing", current))
            if current > max_concurrency:
                max_concurrency = current
            await asyncio.sleep(0.03)

    # Run scheduler dispatch and bypassing caller concurrently
    dispatch_task = asyncio.create_task(scheduler.dispatch_once())
    bypass_task = asyncio.create_task(bypassing_caller())

    await asyncio.gather(dispatch_task, bypass_task)
    await fut

    # Even though bypassing caller skipped the scheduler, concurrency NEVER exceeded 1
    assert max_concurrency == 1
    assert all(count == 1 for _name, count in concurrency_log)


@pytest.mark.asyncio
async def test_cooperative_cancellation_at_dispatch_boundary_non_preemptive():
    """Criterion 5: Cancellation is cooperative at dispatch boundaries; no preemption of in-flight work."""
    scheduler = CapacityAwareScheduler(max_workers=1)
    running_executed = False
    cancelled_executed = False

    async def running_unit():
        nonlocal running_executed
        running_executed = True
        await asyncio.sleep(0.02)
        return "completed"

    async def cancelled_unit():
        nonlocal cancelled_executed
        cancelled_executed = True
        return "should_not_run"

    fut1 = await scheduler.submit("task_1", "owner", "interactive", running_unit)
    fut2 = await scheduler.submit("task_2", "owner", "interactive", cancelled_unit)

    # Request cancellation for task_2 while it is still queued
    was_cancelled = scheduler.request_cancellation("task_2")
    assert was_cancelled is True

    # Dispatch task_1: it runs non-preemptively to completion
    await scheduler.dispatch_once()
    assert running_executed is True
    assert await fut1 == "completed"

    # Dispatch task_2: cancellation observed at dispatch boundary, unit fn does NOT execute
    await scheduler.dispatch_once()
    assert cancelled_executed is False

    with pytest.raises(asyncio.CancelledError):
        await fut2

    assert scheduler.get_metrics().cancellations_count == 1


@pytest.mark.asyncio
async def test_observability_queue_wait_inference_tool_times():
    """Criterion 6: Queue wait, inference time, tool time, and cancellations are observable."""
    scheduler = CapacityAwareScheduler(max_workers=1)

    async def inference_job():
        await asyncio.sleep(0.01)
        return "inference_done"

    async def tool_job():
        await asyncio.sleep(0.01)
        return "tool_done"

    fut_inf = await scheduler.submit(
        "inf_1", "owner_a", "interactive", inference_job, unit_type="inference"
    )
    fut_tool = await scheduler.submit(
        "tool_1", "owner_b", "background", tool_job, unit_type="tool_step"
    )

    await scheduler.dispatch_once()
    await scheduler.dispatch_once()

    await fut_inf
    await fut_tool

    metrics = scheduler.get_metrics()
    assert len(metrics.queue_wait_times) == 2
    assert all(wait >= 0.0 for wait in metrics.queue_wait_times)
    assert len(metrics.inference_times) == 1
    assert metrics.inference_times[0] >= 0.005
    assert len(metrics.tool_times) == 1
    assert metrics.tool_times[0] >= 0.005
    assert metrics.dispatches_by_lane == {"interactive": 1, "background": 1}
    assert metrics.dispatches_by_owner == {"owner_a": 1, "owner_b": 1}
    assert len(metrics.dispatch_history) == 2


@pytest.mark.asyncio
async def test_interactive_and_background_share_same_capacity_controller():
    """Criterion 1: Interactive and background paths use the same capacity controller

    when they share a provider.
    """
    scheduler = CapacityAwareScheduler(max_workers=1, provider_name="ollama")
    interactive_running = False
    background_running = False
    both_ran_concurrently = False

    async def interactive_job():
        nonlocal interactive_running, both_ran_concurrently
        interactive_running = True
        if background_running:
            both_ran_concurrently = True
        await asyncio.sleep(0.02)
        interactive_running = False

    async def background_job():
        nonlocal background_running, both_ran_concurrently
        background_running = True
        if interactive_running:
            both_ran_concurrently = True
        await asyncio.sleep(0.02)
        background_running = False

    await scheduler.submit("i1", "owner_1", "interactive", interactive_job)
    await scheduler.submit("b1", "owner_1", "background", background_job)

    # Run two dispatches
    await scheduler.dispatch_once()
    await scheduler.dispatch_once()

    assert both_ran_concurrently is False
    assert scheduler.limiter.active_count == 0


@pytest.mark.asyncio
async def test_queue_limit_per_owner_enforced():
    """Verify that submitting beyond max_queue_per_owner raises BufferError."""
    scheduler = CapacityAwareScheduler(max_queue_per_owner=2)

    async def dummy():
        pass

    await scheduler.submit("1", "owner_x", "interactive", dummy)
    await scheduler.submit("2", "owner_x", "interactive", dummy)

    with pytest.raises(BufferError, match="Queue limit"):
        await scheduler.submit("3", "owner_x", "interactive", dummy)


@pytest.mark.asyncio
async def test_reentrant_provider_slot_prevents_self_deadlock():
    """Verify that a scheduled unit can invoke provider_capacity without deadlocking.

    When max_concurrency=1, if dispatch_once acquires the slot and the unit's
    underlying function also calls provider_capacity("ollama"), re-entrant ContextVar
    tracking ensures it succeeds immediately rather than self-deadlocking.
    """
    scheduler = CapacityAwareScheduler(max_workers=1, provider_name="ollama")
    acquired_inner = False

    async def production_call():
        nonlocal acquired_inner
        async with provider_capacity("ollama"):
            acquired_inner = True
            return "ok"

    fut = await scheduler.submit("unit-1", "user_1", "interactive", production_call)
    dispatched = await asyncio.wait_for(scheduler.dispatch_once(), timeout=1.0)
    assert dispatched is True
    res = await asyncio.wait_for(fut, timeout=1.0)
    assert res == "ok"
    assert acquired_inner is True
    assert scheduler.limiter.active_count == 0


@pytest.mark.asyncio
async def test_provider_limiter_waiter_count_does_not_leak_on_cancellation():
    """Verify that cancelling while waiting for semaphore cleanly decrements waiting_count."""
    limiter = ProviderLimiter("test_leak", max_concurrency=1)

    # First task acquires the single slot
    ctx1 = limiter.acquire()
    await ctx1.__aenter__()
    assert limiter.active_count == 1
    assert limiter.waiting_count == 0

    # Second task blocks waiting in an independent context
    async def blocked_waiter():
        async with limiter.acquire():
            pass

    task2 = asyncio.create_task(blocked_waiter(), context=contextvars.Context())
    await asyncio.sleep(0.01)
    assert limiter.waiting_count == 1

    # Cancel task2 while it is waiting
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2

    # waiting_count must not have leaked
    assert limiter.waiting_count == 0
    assert limiter.active_count == 1

    # Release first slot
    await ctx1.__aexit__(None, None, None)
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_provider_capacity_controller_thread_safe_registry():
    """Verify registry preserves existing limiter instances under concurrent access."""
    controller = ProviderCapacityController.get_instance()
    lim1 = controller.get_limiter("provider_x", max_concurrency=2)
    lim2 = controller.get_limiter("provider_x", max_concurrency=5)
    assert lim1 is lim2
    assert lim1.max_concurrency == 2


@pytest.mark.asyncio
async def test_scheduler_worker_loop_start_and_stop():
    """Verify that background worker loop processes tasks via condition signaling and stops cleanly."""
    scheduler = CapacityAwareScheduler(max_workers=1, provider_name="ollama")
    results = []

    async def job(idx: int):
        results.append(idx)
        return idx

    await scheduler.start()
    assert scheduler.is_running is True

    fut1 = await scheduler.submit("t1", "u1", "interactive", lambda: job(1))
    fut2 = await scheduler.submit("t2", "u1", "background", lambda: job(2))

    res1 = await asyncio.wait_for(fut1, timeout=1.0)
    res2 = await asyncio.wait_for(fut2, timeout=1.0)
    assert res1 == 1
    assert res2 == 2
    assert results == [1, 2]

    await scheduler.stop()
    assert scheduler.is_running is False


@pytest.mark.asyncio
async def test_schedule_work_helper_routes_interactive_and_background():
    """Verify schedule_work helper executes work through the scheduler."""
    async def add(a: int, b: int):
        return a + b

    res_i = await schedule_work("task-i", "user-1", "interactive", lambda: add(2, 3), unit_type="turn")
    res_b = await schedule_work("task-b", "user-1", "background", lambda: add(10, 20), unit_type="inference")

    assert res_i == 5
    assert res_b == 30


@pytest.mark.asyncio
async def test_scheduler_worker_propagates_contextvars():
    """Verify contextvars set by caller are preserved when worker task executes unit."""
    from src.agent.runtime import CURRENT_TASK_ID, CURRENT_TURN_ID

    observed_task_id = None
    observed_turn_id = None

    async def read_context():
        nonlocal observed_task_id, observed_turn_id
        observed_task_id = CURRENT_TASK_ID.get()
        observed_turn_id = CURRENT_TURN_ID.get()
        return "done"

    scheduler = CapacityAwareScheduler(max_workers=1)
    await scheduler.start()

    token_task = CURRENT_TASK_ID.set("task-abc-123")
    token_turn = CURRENT_TURN_ID.set("turn-xyz-789")
    try:
        fut = await scheduler.submit("task-abc-123", "owner_1", "interactive", read_context)
        res = await asyncio.wait_for(fut, timeout=1.0)
        assert res == "done"
        assert observed_task_id == "task-abc-123"
        assert observed_turn_id == "turn-xyz-789"
    finally:
        CURRENT_TASK_ID.reset(token_task)
        CURRENT_TURN_ID.reset(token_turn)
        await scheduler.stop()


@pytest.mark.asyncio
async def test_multi_provider_capacity_isolation():
    """Verify that different providers (e.g. ollama vs kev vs openai) use separate limiters."""
    scheduler = CapacityAwareScheduler(max_workers=2)

    ollama_running = False
    kev_running = False
    ran_concurrently = False

    async def ollama_job():
        nonlocal ollama_running, ran_concurrently
        ollama_running = True
        if kev_running:
            ran_concurrently = True
        await asyncio.sleep(0.03)
        ollama_running = False

    async def kev_job():
        nonlocal kev_running, ran_concurrently
        kev_running = True
        if ollama_running:
            ran_concurrently = True
        await asyncio.sleep(0.03)
        kev_running = False

    await scheduler.start()
    try:
        fut1 = await scheduler.submit("t-ollama", "owner_1", "interactive", ollama_job, provider="ollama")
        fut2 = await scheduler.submit("t-kev", "owner_2", "background", kev_job, provider="kev")

        await asyncio.gather(fut1, fut2)
        assert ran_concurrently is True
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_cancel_owner_work():
    """Verify cancel_owner_work flags all pending queued units for an owner."""
    scheduler = CapacityAwareScheduler(max_workers=1)

    async def dummy():
        return "ok"

    await scheduler.submit("u1_job1", "user_x", "interactive", dummy)
    await scheduler.submit("u1_job2", "user_x", "interactive", dummy)
    await scheduler.submit("u2_job1", "user_y", "interactive", dummy)

    cancelled = scheduler.cancel_owner_work("user_x")
    assert cancelled == 2

    # Dispatch runs: cleans up both cancelled units for user_x and dispatches user_y's job
    dispatched1 = await scheduler.dispatch_once()
    assert dispatched1 is True
    assert scheduler.metrics.cancellations_count == 2
    assert scheduler.metrics.dispatches_by_owner.get("user_y", 0) == 1


@pytest.mark.asyncio
async def test_scheduler_dynamic_arrival_fairness():
    """Verify owner rotation does not drain Owner A's backlog when Owner B submits dynamically."""
    scheduler = CapacityAwareScheduler(max_workers=1, max_active_tasks_per_owner=1)
    execution_order = []

    a1_started = asyncio.Event()
    a1_release = asyncio.Event()

    async def job_a1():
        execution_order.append("A1")
        a1_started.set()
        await a1_release.wait()
        return "a1"

    async def job_a2():
        execution_order.append("A2")
        return "a2"

    async def job_b1():
        execution_order.append("B1")
        return "b1"

    # 1. Owner A submits A1
    fut_a1 = await scheduler.submit("task_a1", "owner_A", "interactive", job_a1)

    # Dispatch A1 (now running in background)
    dispatch_task = asyncio.create_task(scheduler.dispatch_once())
    await a1_started.wait()

    # 2. While A1 is running: Owner A submits A2, then Owner B submits B1
    fut_a2 = await scheduler.submit("task_a2", "owner_A", "interactive", job_a2)
    fut_b1 = await scheduler.submit("task_b1", "owner_B", "interactive", job_b1)

    # 3. Release A1
    a1_release.set()
    await dispatch_task
    assert await fut_a1 == "a1"

    # 4. Next dispatch MUST be B1 (fair rotation, not draining A's backlog)
    assert await scheduler.dispatch_once() is True
    assert await fut_b1 == "b1"

    # 5. Next dispatch is A2
    assert await scheduler.dispatch_once() is True
    assert await fut_a2 == "a2"

    assert execution_order == ["A1", "B1", "A2"]


@pytest.mark.asyncio
async def test_scheduler_global_max_workers_enforced_concurrently():
    """Verify global max_workers ceiling is respected even under concurrent dispatch_once calls."""
    scheduler = CapacityAwareScheduler(max_workers=1)
    current_active = 0
    max_observed_active = 0
    lock = asyncio.Lock()

    async def task_fn():
        nonlocal current_active, max_observed_active
        async with lock:
            current_active += 1
            if current_active > max_observed_active:
                max_observed_active = current_active
        await asyncio.sleep(0.05)
        async with lock:
            current_active -= 1
        return "done"

    for i in range(5):
        await scheduler.submit(f"task_{i}", f"owner_{i}", "interactive", task_fn)

    # Launch 5 concurrent dispatch_once attempts
    await asyncio.gather(*(scheduler.dispatch_once() for _ in range(5)))
    assert max_observed_active == 1


@pytest.mark.asyncio
async def test_schedule_work_caller_cancellation():
    """Verify cancelling caller of schedule_work triggers cooperative cancellation at dispatch boundary."""
    from src.agent.scheduler import schedule_work, reset_global_scheduler, set_global_scheduler

    reset_global_scheduler()
    scheduler = CapacityAwareScheduler(max_workers=1)
    set_global_scheduler(scheduler)

    job1_started = asyncio.Event()
    job1_release = asyncio.Event()

    async def blocking_job():
        job1_started.set()
        await job1_release.wait()
        return "job1"

    async def queued_job():
        return "job2"

    # Start worker so it dispatches blocking_job
    await scheduler.start()
    try:
        task1 = asyncio.create_task(
            schedule_work("t_block", "user_1", "interactive", blocking_job)
        )
        await job1_started.wait()

        # Submit task2 while task1 is running (sits in queue)
        task2 = asyncio.create_task(
            schedule_work("t_cancel", "user_2", "interactive", queued_job)
        )
        await asyncio.sleep(0.01)

        # Cancel task2 while it's still waiting in queue
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2

        # Release task1 and wait for it
        job1_release.set()
        assert await task1 == "job1"

        # Worker now encounters task2 at dispatch boundary and records cancellation
        await asyncio.sleep(0.05)
        assert scheduler.metrics.cancellations_count == 1
    finally:
        await scheduler.stop()
        reset_global_scheduler()


@pytest.mark.asyncio
async def test_queue_advisor_item_does_not_block_on_background_work():
    """Verify _queue_advisor_item allows interactive items even if background lane has items."""
    from src.bot.commands import _queue_advisor_item, PENDING_ADVISOR_MESSAGES, MAX_PENDING_ADVISOR_MESSAGES
    from src.agent.scheduler import get_global_scheduler, reset_global_scheduler, WorkUnit

    reset_global_scheduler()
    scheduler = get_global_scheduler()
    owner_q = scheduler.get_owner_queue("test_user_lane")

    # Put 8 background items in the scheduler queue
    async def dummy():
        pass

    for i in range(8):
        owner_q.background_lane.append(
            WorkUnit(
                task_id=f"bg_{i}", owner_id="test_user_lane", lane="background",
                fn=dummy, unit_type="inference",
            )
        )

    PENDING_ADVISOR_MESSAGES.pop("test_user_lane", None)
    try:
        # Should succeed because interactive lane is empty
        res = _queue_advisor_item("test_user_lane", {"prompt": "hello", "images": []})
        assert res == 1

        # Direct submission to scheduler for interactive lane must also succeed without BufferError
        fut = await scheduler.submit("interactive_1", "test_user_lane", "interactive", dummy)
        assert fut is not None
        assert len(owner_q.interactive_lane) == 1
        assert len(owner_q.background_lane) == 8
    finally:
        PENDING_ADVISOR_MESSAGES.pop("test_user_lane", None)
        reset_global_scheduler()


@pytest.mark.asyncio
async def test_interactive_stream_and_kev_decide_nested_execution_no_deadlock():
    """Verify interactive model inference and Kev decision run sequentially without deadlocking max_workers=1."""
    from src.agent.scheduler import schedule_work, reset_global_scheduler, set_global_scheduler

    reset_global_scheduler()
    scheduler = CapacityAwareScheduler(max_workers=1, max_active_tasks_per_owner=1)
    set_global_scheduler(scheduler)
    await scheduler.start()

    events = []

    async def simulate_turn():
        # Round 1: Model inference scheduled as interactive inference unit
        async def _model_inference_1():
            events.append("model_round_1")
            return "call_kev_decision"

        res1 = await schedule_work("inf_1", "user_turn", "interactive", _model_inference_1, unit_type="inference", provider="ollama")
        assert res1 == "call_kev_decision"

        # Tool step: Kev decision scheduled as inference unit
        async def _kev_decision():
            events.append("kev_decide")
            return "execute_tool"

        res2 = await schedule_work("kev_1", "user_turn", "interactive", _kev_decision, unit_type="inference", provider="kev")
        assert res2 == "execute_tool"

        # Round 2: Final Model inference scheduled as interactive inference unit
        async def _model_inference_2():
            events.append("model_round_2")
            return "final_answer"

        res3 = await schedule_work("inf_2", "user_turn", "interactive", _model_inference_2, unit_type="inference", provider="ollama")
        return res3

    try:
        # Run with timeout to immediately catch any deadlock
        final = await asyncio.wait_for(simulate_turn(), timeout=2.0)
        assert final == "final_answer"
        assert events == ["model_round_1", "kev_decide", "model_round_2"]
    finally:
        await scheduler.stop()
        reset_global_scheduler()


@pytest.mark.asyncio
async def test_summarize_history_block_scheduler_routed(monkeypatch):
    """Verify _summarize_history_block accepts uid and routes work through scheduler."""
    from src.services.llm import _summarize_history_block
    from src.agent.scheduler import CapacityAwareScheduler, set_global_scheduler, reset_global_scheduler

    reset_global_scheduler()
    scheduler = CapacityAwareScheduler(max_workers=1)
    set_global_scheduler(scheduler)

    # Mock httpx response to avoid real network call
    class DummyResponse:
        status_code = 200
        def json(self):
            return {"message": {"content": "summary digest text"}}

    class DummyClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            return DummyResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: DummyClient())
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    try:
        res = await _summarize_history_block("user: hello\nassistant: hi", uid="test_user_summary")
        assert res == "summary digest text"
        assert scheduler.metrics.dispatches_by_owner.get("test_user_summary", 0) == 1
    finally:
        reset_global_scheduler()


@pytest.mark.asyncio
async def test_qdrant_get_embeddings_gated_with_provider_limiter(monkeypatch):
    """Verify batch get_embeddings in qdrant_client acquires provider_capacity(ollama)."""
    from src.services import qdrant_client
    from src.agent.scheduler import get_provider_limiter

    limiter = get_provider_limiter("ollama")
    acquired = False

    class DummyResponse:
        status_code = 200
        def json(self):
            return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    class DummyClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            nonlocal acquired
            # Check limiter active count is > 0 while post is executing
            if limiter.active_count > 0:
                acquired = True
            return DummyResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: DummyClient())
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")

    res = await qdrant_client.get_embeddings(["text 1", "text 2"])
    assert len(res) == 2
    assert acquired is True




