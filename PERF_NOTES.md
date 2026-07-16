# Performance notes - parallel rendering investigation

## Context

This branch (`perf-parallel-rendering`) is a dedicated investigation into parallelizing the Python rendering pipeline. The goal is to measure where time is actually spent, then apply `multiprocessing` to the most impactful bottlenecks and compare the results against this baseline.

## Why multiprocessing and not threading

Python's GIL (Global Interpreter Lock) prevents true parallel execution of CPU-bound Python code across threads. Libraries like numpy, scipy, and embree release the GIL internally (their hot paths are C extensions), but the orchestration code in `renderer.py` and `compositor.py` does not.

`multiprocessing` sidesteps the GIL by launching separate OS processes, each with their own interpreter. The tradeoff is serialization overhead when passing large meshes to child processes, and the fact that pyrender/OpenGL is not thread-safe -- it requires separate processes (one GL context per process), not threads.

## Instrumentation

`app.py` was instrumented with `time.perf_counter()` around each major stage.
Timings are passed back to the frontend in the SSE `done` event payload and displayed in a "Render timings" table below the generated image, visible to any user running the app (not just in server logs). This makes benchmark comparisons reproducible by anyone testing the branch.

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


| Mode           | AO precompute | Views total | Composition | Total  |
| -------------- | ------------- | ----------- | ----------- | ------ |
| No AO          | -             | 10.83s      | 2.29s       | 13.22s |
| Vertex AO      | 3.59s         | 12.26s      | 9.51s       | 25.48s |
| Directional AO | 0.21s         | 11.84s      | 9.41s       | 21.58s |
| SSAO ("Fast")  | -             | 63.40s      | 9.56s       | 74.06s |


Individual view times are very consistent (~1.7-2.1s each without AO, ~10s each with SSAO), which confirms the views are independent and uniform. A good candidate for parallelization.

### Model B -- high-complexity (45 MB .glb, 41 parts, 385,858 faces)

Vertex AO was run twice to rule out background task interference.
Both runs confirm the result is stable (111-140s range, ~16% variance).


| Mode              | AO precompute | Views total | Composition | Total   |
| ----------------- | ------------- | ----------- | ----------- | ------- |
| No AO             | -             | 18.97s      | 5.42s       | 24.78s  |
| Vertex AO (run 1) | 140.68s       | 21.75s      | 13.42s      | 176.28s |
| Vertex AO (run 2) | 111.53s       | 21.94s      | 14.17s      | 148.06s |
| Directional AO    | 17.27s        | 21.06s      | 13.77s      | 52.53s  |
| SSAO ("Fast")     | -             | 71.45s      | 13.59s      | 86.38s  |


### Scaling comparison (Model A -> Model B, 4.6x more faces)


| Mode           | Model A | Model B     | Ratio |
| -------------- | ------- | ----------- | ----- |
| No AO          | 13.22s  | 24.78s      | x1.9  |
| Vertex AO      | 25.48s  | ~162s (avg) | x6.4  |
| Directional AO | 21.58s  | 52.53s      | x2.4  |
| SSAO           | 74.06s  | 86.38s      | x1.2  |


## Key findings

### 1. SSAO is not fast on large models

The "Fast (SSAO)" label in the UI refers to the absence of a bake/precompute step, not to wall-clock speed. SSAO computes occlusion inside each  
`render_view()` call from that view's own depth buffer. On a complex model this costs ~10s per view, totaling ~60s for 6 views, far slower than vertex AO (3.59s precompute + ~12s views = ~16s) on this mesh.

The UI description should probably be updated to clarify this tradeoff.

### 2. Composition is 4x slower when AO is enabled

Without AO: composition takes 2.29s.
With any AO mode: composition takes ~9.5s, regardless of which AO mode was used.

The likely explanation: AO-enabled view results carry per-pixel shading data (float arrays) that matplotlib has to process on top of the base geometry. This is an unexpected bottleneck since composition was assumed to be cheap.

### 3. Directional AO is the cheapest mode overall (small models)

On Model A, precompute is nearly instant (0.21s). On Model B it jumps to 17.27s, which suggests it involves more than a simple dot-product pass. Likely a vertex merge or KD-tree operation that scales super-linearly.

### 4. Vertex AO scales super-linearly with mesh density

Going from 83k to 385k faces (4.6x) pushes vertex AO precompute from 3.59s to ~125s average (x35). The ray-casting complexity is O(V x R x log F) where V = vertices, R = 80 rays per vertex, F = faces. Beyond a mesh density threshold, BVH traversal depth and CPU cache pressure compound each other, producing a much steeper-than-linear curve in practice.

On large models, vertex AO precompute dominates all other stages combined. Parallelizing its chunks across CPU cores becomes the highest priority.

### 5. Vertex AO offers the best quality/time ratio on small models

At 25.48s total on Model A, it is 3x faster than SSAO (74s) while producing per-vertex baked shading. The precompute is a one-time cost independent of the number of views. On Model B the advantage reverses: SSAO (86s) is faster than vertex AO (~162s) despite scaling worse per-view, because its per-view cost does not depend on mesh density.

## Parallelization targets (priority order)

### Target 1 - the 6 view renders (DONE)

Implemented via `ProcessPoolExecutor` in `app.py`. Each `render_view()` call runs in its own process with its own OpenGL context. Progress events are yielded via `as_completed()` as each view finishes.

### Target 2 - AO vertex chunks (DONE)

Implemented via `ProcessPoolExecutor` in `renderer.py`. A new top-level worker function `_ao_chunk_worker` receives raw numpy arrays (vertices + faces) instead of a trimesh object -- safer to pickle across platforms as the embree C++ internals are not guaranteed to survive pickling. trimesh and the embree intersector are re-initialized fresh inside each worker.  
Each chunk gets its own seed (seed + chunk_index) to avoid correlated samples across chunks.

Worker count formula: `round((os.cpu_count() or 4) / 4) + 1`

This was tuned empirically on a 20-core Intel Core Ultra 7 265KF. The key finding: embree already uses multiple threads internally (OpenMP), so  
launching too many worker processes causes CPU oversubscription and degrades performance. Results on Model B (385k faces):


| Workers                | AO precompute | Total |
| ---------------------- | ------------- | ----- |
| Sequential             | ~125s         | ~162s |
| 4 workers              | 97s           | 124s  |
| 6 workers (sweet spot) | 84s           | 112s  |
| 8 workers              | 95s           | 124s  |
| 19 workers (cpu_count) | 98s           | 128s  |
| `round(20/4)+1` = 6    | 87s           | 117s  |


The formula gives 6 on 20 cores, 5 on 16 cores, 4 on 12 cores, 3 on 8 cores, 2 on 4 cores. Validated at ~117s on the test machine (within noise  
of the hardcoded-6 result of 112s).

Note: embree's internal threading behavior may differ between Intel and AMD architectures, or between CPUs with very different core counts and cache sizes. The formula is a reasonable starting point, not a universal optimum. Further tuning by contributors with different hardware is welcome.

### Target 3 - composition (matplotlib)

At ~13-16s with AO enabled on Model B, composition remains the next bottleneck. Not yet parallelized. matplotlib's figure rendering is partially GIL-bound, so gains may be more modest than for the other stages.

## Results after view + AO chunk parallelization

### Model A (4.6 MB, 83k faces)


| Mode           | Sequential | Views // | Views + chunks // | Best gain |
| -------------- | ---------- | -------- | ----------------- | --------- |
| No AO          | 13.22s     | 8.72s    | n/a               | x1.5      |
| Vertex AO      | 25.48s     | 19.93s   | 16.20s            | x1.6      |
| Directional AO | 21.58s     | 15.24s   | n/a               | x1.4      |
| SSAO           | 74.06s     | 26.18s   | n/a               | x2.8      |


### Model B (45 MB, 385k faces)


| Mode           | Sequential | Views // | Views + chunks // | Best gain |
| -------------- | ---------- | -------- | ----------------- | --------- |
| No AO          | 24.78s     | 12.67s   | n/a               | x2.0      |
| Vertex AO      | ~162s      | 147.98s  | ~117s             | x1.4      |
| Directional AO | 52.53s     | 36.38s   | n/a               | x1.4      |
| SSAO           | 86.38s     | 34.52s   | n/a               | x2.5      |


### Key observations

SSAO remains the biggest winner overall: x2.8 on small models, x2.5 on large ones. The per-view cost parallelizes almost perfectly.

AO chunk parallelization is most impactful on small models (x18 on the precompute alone for Model A) because embree's internal threading already saturates available cores on large meshes, limiting process-level gains.

Composition (~13-16s with AO on Model B) is now the dominant remaining bottleneck for vertex and directional modes on large models.

## Output correctness

All four AO modes were tested against vanilla Ortho (origin/main) using the same model and settings. No visual difference was detected between sequential and parallel outputs for No AO, SSAO, and Directional modes.

Vertex AO produces outputs that are visually identical but not pixel-perfect: the parallel version differs by ~3 KB in PNG file size. This is expected and intentional. The sequential version uses a single shared RNG (Random Number Generator) across all chunks, so random samples are continuous across chunk boundaries. The parallel version gives each worker its own seed (seed + chunk index) to avoid correlated samples between chunks. The resulting AO values differ within the noise of the 80-ray hemisphere sampling, visually indistinguishable, and arguably slightly better (less inter-chunk correlation).  
This is a known and accepted tradeoff, not a defect.

## Remaining opportunities

- Composition (matplotlib): ~13-16s with AO, not yet parallelized
- AO chunk worker count: further tuning on AMD, Apple Silicon, and
high-core-count machines (Threadripper, etc.)
- Chunk size (currently 20k vertices): larger chunks may reduce
process startup overhead on large models

 