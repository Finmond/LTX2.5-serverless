# FINMOND bounded Serverless rendering

The `ltx2-5-distilled-int8-bounded` bake target uses the existing pinned model,
workflow, dependencies and image publication path. `FINMOND_BOUNDED_RENDER=1`
enables independent Linux process supervision; ordinary build targets retain
their existing default. Build for linux/amd64 and deploy an immutable digest.

The guardian launches the one private loopback renderer in its own session and
acts as a child subreaper. Before inference the handler must arm one immutable
request lease with `render_timeout_seconds` between 1 and 600. The guardian
enforces its monotonic deadline independently of asyncio, HTTP, the SDK and
Redis. Expiry, handler death or inference failure stops owned inference children,
including children that ignore SIGTERM or create another session. Ownership is
verified with private manifests, PID start times and parent relationships.
Renderer death is terminal for this worker; a later request cannot reuse it.

Bounded workflows never fail over to a second inference attempt. Returning an
error leaves the SDK alive to report the outcome rather than intentionally
crashing it to trigger a provider retry. The public frontend is disabled and
non-workflow or non-local targets are rejected in this mode. Provider TTL or
executionTimeout alone never establish this capability.

These CPU process tests prove the supervision contract, not a successful CUDA
generation or a guaranteed provider invoice cap. Cold startup and artifact
transfer remain billed overhead. An OOM or forced SIGKILL of the guardian cannot
run userspace cleanup; reconcile provider state before recovery. GPU calibration
must record the immutable runtime, actual job IDs, exact workflow/settings,
queue/start/render/transfer timings and genuine provider billing. Aggregate bills
must remain explicitly aggregate; never invent individual receipts.

Before deployment run the focused tests and the existing required CI checks:

```sh
python -m unittest tests.test_comfy_deadline tests.test_bounded_handler -q
bash -n src/start.sh
docker buildx bake --print ltx2-5-distilled-int8-bounded
```

Publish using the existing manual Docker Hub workflow and target above. Do not
restart an always-on Pod, create a new GPU endpoint, increase minimum workers or
enable LIVE payments. Retain min_workers=0 and the existing initial max_workers=1.
