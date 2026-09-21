# Resource-aware worker warm shutdown

Base: `2014924` (the sealed acceptance snapshot is unchanged). Scope: ResourceAwareWorker admission/control waits, worker supervisor lifecycle evidence, focused tests.

## Diagnosis and behavior

Installed RQ is 2.10.0. Its `StopRequested` derives from `Exception`; an idle SIGTERM raises it, while a BUSY SIGTERM sets the warm-stop flag. There is no `check_stop_requested` method in this installed version. The generic control subscriber exception handler swallowed idle shutdown, and custom dequeue/admission loops did not observe the shutdown flag. Saved installed-source evidence: `worker-stop-rq-semantics.log`, `worker-stop-rq-ownership.log`, and `worker-stop-rq-cancel.log`.

Control waits now propagate `StopRequested`; custom loops check RQ's flag/date. An accepted but unstarted job remains BUSY while admission acquires resources, making the signal cooperative until a safe wait boundary. Pubsub waits check the flag at most one second apart without resampling pressure sooner than the existing adaptive delay. At that boundary a Lua operation retires the owned intermediate entry and its first-seen age, then restores the job to the front of its queue only if its durable RQ status is still queued. Cancellation cannot be revived. A signal after admission acquires resources finishes the accepted job through ordinary warm execution and resource release.

An aged admission waiter was also proven to be failed by its own RQ intermediate maintenance. During admission, heartbeat/procline continue and that worker's maintenance is deferred. The old first-seen timestamp is removed on return so the successor does not inherit stale age.

The supervisor emits one-line JSON `worker_started` events with `pid`/`queues`, and `worker_stopped` events with `pid`/`queues`/`return_code`/`forced`. Runtime exits are recorded as well as final shutdown. Both the 55-second kill path and subsequent wait-timeout kill path mark forced termination. Final records follow actual wait/reap; a second reap timeout propagates and prevents the final success message. The supervisor's own zero exit alone is not evidence of normal child exit.

## RED / GREEN evidence

All pytest used `.superpowers/run-tests` and its serialized isolated `ag-latency-runner`, Redis database15, dedicated random queue/worker/job namespaces and temporary local lock paths. Each real subprocess test owns its process group and scheduler, reaps only those processes, and removes only its namespace. No production, NAS acceptance Redis10, sealed source snapshots, queue resets, deployment, or full-suite run occurred.

- `worker-stop-red.log`: **3 failed in 22.84s**. Real idle, pressure-paused and accepted-admission workers all remained alive past the 5-second SIGTERM deadline.
- `worker-stop-supervisor-red.log`: **2 failed, 10 deselected in 0.28s**. Missing child exit evidence.
- `worker-stop-supervisor-red-expanded.log`: **3 failed, 10 deselected in 0.29s**, including delayed reap requiring the second kill path.
- `worker-stop-acquire-red.log`: **1 failed, 4 deselected in 4.55s**. A real signal after local lock acquisition during resumed admission returned the job rather than warm-completing it because bookkeeping had reset IDLE.
- `worker-stop-maintenance-red.log`: **1 failed, 5 deselected in 1.11s**. Real RQ cleanup with a controlled 120-second-old first-seen timestamp marked the live waiter failed.
- `worker-stop-cancel-red.log`: **1 failed, 6 deselected in 2.37s**. Actual RQ cancellation left the accepted job in intermediate after shutdown.
- Initial `worker-stop-green-initial.log`: **3 passed in 6.27s**.
- Expanded `worker-stop-green.log`: **19 passed in 16.50s**.
- Final `worker-stop-focused.log`: **52 passed in 19.12s**, command:
  `.superpowers/run-tests tests/test_resource_worker_shutdown.py tests/test_resource_aware_worker.py tests/test_worker_concurrency.py tests/test_worker_health_probe.py -q`
- `worker-stop-ruff.log`: **All checks passed!**, serialized `python -m ruff check --no-cache` on the four changed Python files. `git diff --check` passed.

The seven real-worker scenarios assert RQ membership removal, no scheduler/workhorse process-group leftovers where applicable, no new failed jobs, queued return followed by exactly one successful successor execution, running-job warm completion, post-acquisition completion, aged own-maintenance protection, and canceled-job retirement without resurrection. Normal signal waits have a 5-second bound; the entire final focused set completed in 19.12 seconds. Supervisor clocks/processes are controlled test doubles to exercise both long timeout branches without imposing 55-second test waits.

## Limits and release evidence

The approved deployment has one parent RQ worker per queue (including downloads cap1; maintenance is a distinct listener queue). Deferring own maintenance does not solve the pre-existing case where another worker on the same queue cleans an admission waiter's intermediate entry. Acceptance/release must verify this single-worker-per-queue inventory; arbitrary same-queue concurrency is not validated here. Redis failure during the atomic return remains an operational recovery condition rather than proof of a completed return.

The real lifecycle tests substitute only resource-pressure inputs, worker telemetry/domain projection and namespace-safe maintenance boundaries; RQ, Redis, signals, scheduler and workhorse processes are real. They are focused correctness evidence, not a rerun of the NAS mixed pipeline. Root must refresh the candidate, rerun live mixed acceptance and require child exit/registration/process-group evidence before a final success artifact. No previous mixed result or pending release gate is changed by this report.

Independent review: `search_delivery_review` approved the final diff and saved 52-pass/Ruff evidence with no remaining Critical/Important findings for the explicitly verified single-parent-per-queue deployment scope. No reviewer reruns or service mutations occurred.
