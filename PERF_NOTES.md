# Performance notes -- parallel rendering investigation

## Context

This branch (`perf-parallel-rendering`) is a dedicated investigation into
parallelizing the Python rendering pipeline. The goal is to measure where
time is actually spent, then apply `multiprocessing` to the most impactful
bottlenecks and compare the results against this baseline.

## Why multiprocessing and not threading

Python's GIL (Global Interpreter Lock) prevents true parallel execution of
CPU-bound Python code across threads. Libraries like numpy, scipy, and embree
release the GIL internally (their hot paths are C extensions), but the
orchestration code in `renderer.py` and `compositor.py` does not.

`multiprocessing` sidesteps the GIL by launching separate OS processes, each
with their own interpreter. The tradeoff is serialization overhead when passing
large meshes to child processes, and the fact that pyrender/OpenGL is not
thread-safe -- it requires separate processes (one GL context per process),
not threads.

## Instrumentation

`app.py` was instrumented with `time.perf_counter()` around each major stage.
Timings are passed back to the frontend in the SSE `done` event payload and
displayed in a "Render timings" table below the generated image, visible to
any user running the app (not just in server logs). This makes benchmark
comparisons reproducible by anyone testing the branch.

Stages measured individually:
- AO precompute (vertex and directional modes only)
- Each view render (front, back, left, right, top, bottom) individually
- Views total
- SSAO finalize (normalization pass across all views)
- Cross-sections (rib cuts)
- Composition (matplotlib layout + PNG export)
- Grand total

## Baseline measurements

Test model: a mid-complexity car mesh (Assetto Corsa KN5), 6 views, cross-sections enabled.

| Mode | AO precompute | Views total | Composition | Total |
|---|---|---|---|---|
| No AO | - | 10.83s | 2.29s | 13.22s |
| Vertex AO | 3.59s | 12.26s | 9.51s | 25.48s |
| Directional AO | 0.21s | 11.84s | 9.41s | 21.58s |
| SSAO ("Fast") | - | 63.40s | 9.56s | 74.06s |

Individual view times are very consistent (~1.7-2.1s each without AO, ~10s
each with SSAO), which confirms the views are independent and uniform --
a good candidate for parallelization.

## Key findings

### 1. SSAO is not fast on large models

The "Fast (SSAO)" label in the UI refers to the absence of a bake/precompute
step, not to wall-clock speed. SSAO computes occlusion inside each
`render_view()` call from that view's own depth buffer. On a complex model
this costs ~10s per view, totaling ~60s for 6 views -- far slower than vertex
AO (3.59s precompute + ~12s views = ~16s) on this mesh.

The UI description should probably be updated to clarify this tradeoff.

### 2. Composition is 4x slower when AO is enabled

Without AO: composition takes 2.29s.
With any AO mode: composition takes ~9.5s, regardless of which AO mode was used.

The likely explanation: AO-enabled view results carry per-pixel shading data
(float arrays) that matplotlib has to process on top of the base geometry.
This is an unexpected bottleneck -- composition was assumed to be cheap.

### 3. Directional AO is the cheapest mode overall

Precompute is nearly instant (0.21s, a single dot-product pass per vertex).
Total is 21.58s, only 8s more than no AO. The composition overhead (~9.4s)
dominates its cost.

### 4. Vertex AO offers the best quality/time ratio

At 25.48s total, it is 3x faster than SSAO (74s) on this model while
producing per-vertex baked shading. The precompute (3.59s) is a one-time
cost that does not scale with the number of views.

## Parallelization targets (priority order)

### Target 1 -- the 6 view renders (highest ROI for SSAO)

Each `render_view()` call is fully independent. With `ProcessPoolExecutor`
and 6 workers the theoretical speedup on the views phase is up to 6x.
For SSAO specifically this would cut ~60s down to ~10s (the cost of the
slowest single view).

Constraint: pyrender/OpenGL requires separate OS processes (not threads)
because each process needs its own GL context.

Constraint: the SSE progress stream in `app.py` sends per-view progress
events. With parallel rendering these need to be collected via a `Queue`
shared between the main process and the worker processes.

Constraint: the mesh object must be serialized (pickled) to each worker
process. For very large meshes this adds overhead that partially offsets
the gain.

### Target 2 -- composition (matplotlib)

At ~9.5s with AO enabled, composition is worth investigating. The 6 view
figures are independent and could be rendered in parallel with
`ProcessPoolExecutor` before being assembled into the final composite.
However, matplotlib's figure rendering is partially GIL-bound, so gains
may be more modest than for the view renders.

### Target 3 -- AO vertex chunks

`compute_ambient_occlusion()` already processes vertices in chunks of 20k.
These chunks could be distributed across a process pool. Lower priority
because the current 3.59s is already acceptable.

## Next step

Implement parallel view rendering with `ProcessPoolExecutor`, keeping the
SSE progress stream functional via a `multiprocessing.Queue`. Measure the
result against this baseline using the same model and same settings.
