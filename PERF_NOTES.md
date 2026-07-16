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

All runs: 6 views (front/back/left/right/top/bottom) + cross-sections enabled.
Machine: Intel Core Ultra 7 265KF @ 3.90 GHz, 32 GB RAM, Windows 11.

### Model A -- mid-complexity (4.6 MB .glb, 44 parts, 83,430 faces)

| Mode | AO precompute | Views total | Composition | Total |
|---|---|---|---|---|
| No AO | - | 10.83s | 2.29s | 13.22s |
| Vertex AO | 3.59s | 12.26s | 9.51s | 25.48s |
| Directional AO | 0.21s | 11.84s | 9.41s | 21.58s |
| SSAO ("Fast") | - | 63.40s | 9.56s | 74.06s |

Individual view times are very consistent (~1.7-2.1s each without AO, ~10s
each with SSAO), which confirms the views are independent and uniform --
a good candidate for parallelization.

### Model B -- high-complexity (45 MB .glb, 41 parts, 385,858 faces)

Vertex AO was run twice to rule out background task interference.
Both runs confirm the result is stable (111-140s range, ~16% variance).

| Mode | AO precompute | Views total | Composition | Total |
|---|---|---|---|---|
| No AO | - | 18.97s | 5.42s | 24.78s |
| Vertex AO (run 1) | 140.68s | 21.75s | 13.42s | 176.28s |
| Vertex AO (run 2) | 111.53s | 21.94s | 14.17s | 148.06s |
| Directional AO | 17.27s | 21.06s | 13.77s | 52.53s |
| SSAO ("Fast") | - | 71.45s | 13.59s | 86.38s |

### Scaling comparison (Model A -> Model B, 4.6x more faces)

| Mode | Model A | Model B | Ratio |
|---|---|---|---|
| No AO | 13.22s | 24.78s | x1.9 |
| Vertex AO | 25.48s | ~162s (avg) | x6.4 |
| Directional AO | 21.58s | 52.53s | x2.4 |
| SSAO | 74.06s | 86.38s | x1.2 |

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

### 3. Directional AO is the cheapest mode overall (small models)

On Model A, precompute is nearly instant (0.21s). On Model B it jumps to
17.27s, which suggests it involves more than a simple dot-product pass --
likely a vertex merge or KD-tree operation that scales super-linearly.

### 4. Vertex AO scales super-linearly with mesh density

Going from 83k to 385k faces (4.6x) pushes vertex AO precompute from 3.59s
to ~125s average (x35). The ray-casting complexity is O(V x R x log F) where
V = vertices, R = 80 rays per vertex, F = faces. Beyond a mesh density
threshold, BVH traversal depth and CPU cache pressure compound each other,
producing a much steeper-than-linear curve in practice.

On large models, vertex AO precompute dominates all other stages combined.
Parallelizing its chunks across CPU cores becomes the highest priority.

### 5. Vertex AO offers the best quality/time ratio on small models

At 25.48s total on Model A, it is 3x faster than SSAO (74s) while producing
per-vertex baked shading. The precompute is a one-time cost independent of
the number of views. On Model B the advantage reverses: SSAO (86s) is faster
than vertex AO (~162s) despite scaling worse per-view, because its per-view
cost does not depend on mesh density.

## Parallelization targets (priority order)

### Target 1 -- the 6 view renders (DONE)

Implemented via `ProcessPoolExecutor` in `app.py`. Each `render_view()` call
runs in its own process with its own OpenGL context. Progress events are
yielded via `as_completed()` as each view finishes.

### Target 2 -- AO vertex chunks (highest remaining priority)

`compute_ambient_occlusion()` already processes vertices in chunks of 20k.
These chunks could be distributed across a process pool. On large models
(~125s precompute) this is the single biggest remaining win -- view
parallelization barely moves the needle when precompute dominates.

### Target 3 -- composition (matplotlib)

At ~10-16s with AO enabled on large models, composition is worth
investigating. However, matplotlib's figure rendering is partially
GIL-bound, so gains may be more modest than for the view renders.

## Results after view parallelization

### Model A (4.6 MB, 83k faces)

| Mode | Sequential | Parallel | Gain |
|---|---|---|---|
| No AO | 13.22s | 8.72s | x1.5 |
| Vertex AO | 25.48s | 19.93s | x1.3 |
| Directional AO | 21.58s | 15.24s | x1.4 |
| SSAO | 74.06s | 26.18s | x2.8 |

### Model B (45 MB, 385k faces)

| Mode | Sequential | Parallel | Gain |
|---|---|---|---|
| No AO | 24.78s | 12.67s | x2.0 |
| Vertex AO | ~162s | 147.98s | x1.1 |
| Directional AO | 52.53s | 36.38s | x1.4 |
| SSAO | 86.38s | 34.52s | x2.5 |

### Key observations

SSAO is the biggest winner: x2.8 on small models, x2.5 on large ones. The
per-view cost (~10-14s) is substantial and parallelizes almost perfectly.

No AO scales better on large models (x2.0) than small (x1.5): with heavier
geometry per view, the render cost dominates the pickle overhead more cleanly.

Vertex AO is nearly unchanged on large models (x1.1) because the precompute
(117s) eclipses the views (14s). Parallelizing the AO chunks is the only
meaningful path forward for this mode on high-polygon meshes.

Composition (~10-16s with AO) is now the dominant cost for vertex and
directional modes on Model A, and a significant fraction on Model B.
It is not yet parallelized.

## Next step

Parallelize `compute_ambient_occlusion()` vertex chunks across a process pool
to address the vertex AO bottleneck on large models.
