"""
renderer.py — Consolidated rendering logic for the orthographic template app.

Takes a clean trimesh.Trimesh (as returned by model_loader.load_model /
build_filtered_mesh) and produces line-art data for any of the 6 standard
orthographic views plus the width-wise "rib" cross-section, with optional
Lambertian shading-contour detail.

This module is the single place that knows about: depth-buffer hidden-line
removal, the pyrender orthographic-depth bug fix, vertex welding, feature-
edge extraction, silhouette contours, and shading contours. Everything here
was developed and verified against real files earlier in the project — see
STATUS.md for the history. This file consolidates that into clean,
reusable functions instead of the copy-pasted scratch-script style used
during development.
"""

import os
import sys
if "PYOPENGL_PLATFORM" not in os.environ and sys.platform.startswith("linux"):
    os.environ["PYOPENGL_PLATFORM"] = "egl"

import numpy as np
import trimesh
import pyrender
from skimage import measure
from scipy.spatial import cKDTree
from scipy.ndimage import gaussian_filter

from depth_render import make_camera_pose, render_depth
from visibility import compute_visible_edges, pyrender_depth_to_true_distance, project_points


# Shared by all three AO modes (vertex, ssao, directional): the darkest
# any AO/shading darkness value is ever allowed to reach, even at the
# max-darkness slider (100%). Keeps a sliver of brightness at the very
# darkest points so the underlying line art never gets fully obscured.
# ao_max_darkness (0..1, from the 0-100% UI slider) is a SCALE on top of
# this ceiling, not a floor/clamp -- darkness = AO_DARKNESS_CEILING *
# ao_max_darkness * <mode's own 0..1 occlusion/shadow term>. Lowering the
# slider compresses the whole curve toward full brightness proportionally
# (preserves relative differences / dynamic range between values) rather
# than clipping a range of values down to one flat floor.
AO_DARKNESS_CEILING = 0.9

AXIS_VECTORS = {
    "x": np.array([1.0, 0.0, 0.0]),
    "y": np.array([0.0, 1.0, 0.0]),
    "z": np.array([0.0, 0.0, 1.0]),
}


class AxisConfig:
    """Describes which world axis is 'up' and which is the model's
    front-back ('forward') axis, plus the sign of the forward direction.

    Defaults match every file tested so far (Y-up, Z-forward), but BeamNG
    exports in particular are not guaranteed to follow this — user-reported
    real models loading with the wrong orientation. This makes both the
    up-axis and the front sign explicit, overridable settings instead of
    values hardcoded throughout the view/cut geometry.
    """

    def __init__(self, up_axis="y", forward_axis="z", front_sign=1):
        if up_axis == forward_axis:
            raise ValueError("up_axis and forward_axis must be different.")
        self.up_axis = up_axis
        self.forward_axis = forward_axis
        self.front_sign = front_sign

    @property
    def up_vec(self):
        return AXIS_VECTORS[self.up_axis]

    @property
    def forward_vec(self):
        return AXIS_VECTORS[self.forward_axis] * self.front_sign

    @property
    def side_axis(self):
        """The third axis, perpendicular to up and forward."""
        all_axes = {"x", "y", "z"}
        return next(iter(all_axes - {self.up_axis, self.forward_axis}))

    def axis_index(self, axis_name):
        return {"x": 0, "y": 1, "z": 2}[axis_name]


# Standard view definitions, expressed relative to an AxisConfig rather than
# a hardcoded Y-up/Z-forward assumption. (Earlier version of this function
# hardcoded up=[0,1,0] and Z as forward everywhere -- fine for every file
# tested so far, but a real user-reported BeamNG model loaded with the
# wrong orientation, since BeamNG exports don't guarantee this convention.
# AxisConfig makes both overridable from the UI instead of only being
# guessable from part names.)
def _view_geometry(view_name, center, dist, axis_cfg):
    up_vec = axis_cfg.up_vec
    fwd_vec = axis_cfg.forward_vec
    side_idx = axis_cfg.axis_index(axis_cfg.side_axis)
    side_vec = np.zeros(3)
    side_vec[side_idx] = 1.0

    views = {
        # Camera must sit on the SAME side as the front and look back
        # toward the model (sign convention verified during development —
        # see git history / earlier STATUS.md notes on the front/back
        # camera-placement bug).
        "front":  (center - fwd_vec * dist, up_vec),
        "back":   (center + fwd_vec * dist, up_vec),
        "left":   (center - side_vec * dist, up_vec),
        "right":  (center + side_vec * dist, up_vec),
        "top":    (center + up_vec * dist, -fwd_vec),
        "bottom": (center - up_vec * dist, fwd_vec),
    }
    if view_name not in views:
        raise ValueError(f"Unknown view '{view_name}'. Valid: {list(views.keys())}")
    return views[view_name]


def detect_front_axis(mesh, hint_names=None, forward_axis="z"):
    """Best-effort guess at which direction along forward_axis is 'front'.

    Heuristic: look for parts whose names suggest front/rear (e.g. contain
    'front'/'bump' vs 'rear'/'back'/'tail') and compare their average
    position along forward_axis; whichever side the 'front'-ish parts
    cluster on is +front. Falls back to +1 (arbitrary) if no naming signal
    is available — the UI should let the user flip this with one click
    rather than trust it blindly, since this is a genuine per-model
    judgment call (confirmed: Probox front=-Z, Holden VY front=+Z, no
    universal convention exists; some BeamNG models give no usable naming
    signal at all and need the manual flip).

    hint_names: optional dict of {part_name: (face_start, face_end)} so this
    can use real part names; if None, returns +1 with no attempt to guess.
    """
    if not hint_names:
        return 1

    axis_i = {"x": 0, "y": 1, "z": 2}[forward_axis]
    front_pos = []
    back_pos = []
    verts = mesh.vertices
    n_faces = len(mesh.faces)
    for name, (start, end) in hint_names.items():
        nl = name.lower()
        is_front = any(k in nl for k in ("front", "bump_f", "bumper_f", "fbump", "hood", "bonnet"))
        is_back = any(k in nl for k in ("rear", "back", "bump_r", "bumper_r", "rbump", "trunk", "tail"))
        if not (is_front or is_back):
            continue
        # Defensive bounds check: hint_names' offsets only make sense against
        # the SAME mesh they were computed from. If a caller accidentally
        # passes a filtered/different mesh (a real bug hit during
        # development: part_face_ranges from the unfiltered mesh was used
        # to index into a filtered one, which has fewer faces after
        # exclusions and threw IndexError), skip out-of-range entries
        # instead of crashing -- correctness here is best-effort heuristic
        # anyway, a partial guess is better than a hard crash.
        if start < 0 or end > n_faces or start >= end:
            continue
        face_idx = np.arange(start, end)
        vert_idx = np.unique(mesh.faces[face_idx].flatten())
        avg_pos = verts[vert_idx][:, axis_i].mean()
        (front_pos if is_front else back_pos).append(avg_pos)

    if not front_pos or not back_pos:
        return 1
    return 1 if np.mean(front_pos) < np.mean(back_pos) else -1


def get_model_scale(mesh, margin=1.10):
    """Returns (center, half_span, dist) for consistent cross-view framing.
    half_span is derived from the SINGLE LARGEST dimension across the whole
    model and used identically for every view's camera — confirmed essential
    for true relative scale between panels (fitting each view independently
    was an early mistake during development that broke this)."""
    bmin, bmax = mesh.bounds
    center = (bmin + bmax) / 2.0
    extent = bmax - bmin
    max_extent = max(extent)
    half_span = (max_extent / 2.0) * margin
    dist = max_extent * 3
    return center, half_span, dist


def rotate_mesh_around_up_axis(mesh, axis_cfg, degrees):
    """Rotates a COPY of mesh by `degrees` around the up axis (in-place
    around the mesh's own centroid, so the model doesn't drift off-center).

    Added for a real gap: AxisConfig's up_axis/forward_axis/front_sign
    settings can only choose between camera directions that are aligned to
    the world's X/Y/Z axes. A model that's genuinely Y-up (so up_axis="y"
    is correct) but whose front faces some OTHER direction within the
    XZ plane — not aligned to +-X or +-Z at all, e.g. rotated 35 degrees,
    or even a clean 90 degrees in a way that front_flip's sign-only flip
    can't fix — has no axis/sign combination that corrects it; every
    camera-facing direction in render_view() is locked to a world axis.
    User-reported real case: a model needed a 90-degree turn to align,
    previously requiring opening it in Blender, rotating, and re-exporting
    just to use this app — which defeats the point of a quick reference
    tool. This makes that an in-app slider instead.

    degrees=0 is a no-op (returns an unrotated copy, not a no-op skip,
    so callers can always treat the return value uniformly).
    """
    rotated = mesh.copy()
    if degrees == 0:
        return rotated
    center = rotated.vertices.mean(axis=0)
    angle_rad = np.radians(degrees)
    rotation_matrix = trimesh.transformations.rotation_matrix(
        angle_rad, axis_cfg.up_vec, point=center
    )
    rotated.apply_transform(rotation_matrix)
    return rotated


def compute_part_centroids(mesh, part_face_ranges):
    """Returns {part_name: 3D centroid} for labeling parts in a composed
    view. Centroid is the mean of that part's face centroids (not vertex
    mean, which would bias toward whichever end of the part has denser
    triangulation) — a reasonable single anchor point for a numbered label,
    not a claim about the part's true geometric center for oddly-shaped
    parts.

    part_face_ranges must already be in `mesh`'s own face index space —
    i.e. the output of model_loader.remap_part_face_ranges() if mesh is a
    filtered/excluded mesh, not the raw ranges from the original load.
    """
    face_centroids = mesh.triangles.mean(axis=1)  # (n_faces, 3)
    centroids = {}
    for name, (start, end) in part_face_ranges.items():
        if end <= start:
            continue
        centroids[name] = face_centroids[start:end].mean(axis=0)
    return centroids


def project_part_labels(part_centroids, pose, xmag, ymag, resolution):
    """Projects {part_name: 3D point} into this view's pixel space, using
    the same camera params render_view() returns per-view. Returns
    {part_name: (px, py)}. No occlusion/visibility filtering is done here —
    every part gets a label position regardless of what's in front of it in
    this particular view. Fine for an assembly-diagram-style overview;
    would need real per-part visibility testing (extra raycasts) to hide
    labels for parts fully hidden behind others in a given view, which
    hasn't been built yet.
    """
    if not part_centroids:
        return {}
    names = list(part_centroids.keys())
    pts = np.array([part_centroids[n] for n in names])
    px, py, _ = project_points(pts, pose, xmag, ymag, resolution)
    return {name: (float(px[i]), float(py[i])) for i, name in enumerate(names)}


def _smooth_closed_contour(coords, iterations=2):
    """Chaikin corner-cutting smoothing for a closed polyline (HANDOVER_1-29).

    `coords` is an Nx2 array of (row, col) pixel coordinates as returned by
    skimage.measure.find_contours, where coords[0] == coords[-1] (closed
    loop -- true for every outer silhouette contour, since they trace the
    boundary of a filled mask region).

    Why this is needed: find_contours does marching-squares on the raster
    mask, so the output polyline is only as fine as the pixel grid -- on
    shallow-angle / near-horizontal curves this shows up as a visible
    staircase, even though skimage already sub-pixel-interpolates each
    individual crossing. Chaikin's algorithm repeatedly replaces each edge
    with two new points 1/4 and 3/4 along it, which rounds off the corners
    introduced by the grid without needing to know anything about the
    original geometry -- cheap, dependency-free (just numpy), and it
    converges towards a smooth curve that still hugs the original shape
    closely at a handful of iterations (too many start rounding off real,
    sharp silhouette corners that should stay sharp, e.g. where a panel
    line meets the outer edge).

    Only applied to outer_contours (raster-derived). edge_segments (feature/
    crease lines) are already exact vector lines from mesh geometry and are
    left untouched -- they were never jagged to begin with.
    """
    if len(coords) < 8:
        return coords  # too short for smoothing to be meaningful/safe
    pts = coords
    is_closed = np.allclose(pts[0], pts[-1])
    for _ in range(iterations):
        p = pts[:-1] if is_closed else pts
        n = len(p)
        if n < 4:
            break
        q = 0.75 * p + 0.25 * np.roll(p, -1, axis=0)
        r = 0.25 * p + 0.75 * np.roll(p, -1, axis=0)
        new_pts = np.empty((2 * n, 2), dtype=p.dtype)
        new_pts[0::2] = q
        new_pts[1::2] = r
        if is_closed:
            pts = np.vstack([new_pts, new_pts[0:1]])
        else:
            pts = new_pts
    return pts


def render_view(mesh, view_name, center, half_span, dist, axis_cfg,
                 resolution=1800, n_samples=30, depth_eps=0.018,
                 crease_angle_deg=25.0, ao_mesh=None, ao_levels=9,
                 min_contour_area=60, ao_blur_sigma_px=1.4,
                 ao_mode=None, ao_max_darkness=0.5,
                 smooth_contours=True, smooth_iterations=1,
                 contour_supersample=4):
    """Renders one orthographic view of mesh, returning a dict with:
        outer_contours: list of Nx2 pixel-space silhouette contours
        ao_raster: (gray uint8 array, alpha bool mask) for AO shading as a
            multiply-blended raster underlay, or None unless AO was requested
        edge_segments: list of (p0_world, p1_world) visible feature/boundary edges
        pose, xmag, ymag, resolution: camera params needed to project edge_segments to pixels

    axis_cfg: an AxisConfig describing up/forward axes and front sign.
    ao_mesh: optional, a mesh with AO baked in as vertex colors (see
    compute_ambient_occlusion() below), used for ao_mode="vertex". Computed
    ONCE per render request and passed into every view's render_view() call,
    since per-vertex AO is a property of the mesh's geometry, not of any
    particular camera angle — recomputing it per-view would be needlessly
    slow for an identical result.
    ao_mode: None (no AO), "vertex" (use ao_mesh's baked vertex colors), or
    "ssao" (screen-space AO computed directly from THIS view's own depth
    buffer — no ao_mesh needed, genuinely per-view, recomputed every call).
    """
    znear, zfar = 0.01, dist * 2.2
    eye, up = _view_geometry(view_name, center, dist, axis_cfg)

    # Feature edges (dihedral angle) + true mesh boundaries. On fragmented
    # meshes this may find almost nothing — that's expected and handled
    # gracefully (silhouette contours still carry the result), not an error.
    mesh_welded = mesh.copy()
    mesh_welded.merge_vertices(merge_tex=False, merge_norm=False)
    try:
        angles = mesh_welded.face_adjacency_angles
        edges_adj = mesh_welded.face_adjacency_edges
        feature_edges = edges_adj[angles > np.radians(crease_angle_deg)]
        boundary_edges = mesh_welded.edges[
            trimesh.grouping.group_rows(mesh_welded.edges_sorted, require_count=1)
        ]
        edges_v = np.vstack([feature_edges, boundary_edges]) if len(feature_edges) or len(boundary_edges) else np.empty((0, 2), dtype=int)
    except Exception:
        edges_v = np.empty((0, 2), dtype=int)

    if len(edges_v) > 0:
        segs, depth_true, pose, cam = compute_visible_edges(
            mesh_welded, edges_v, eye, center, up, half_span, half_span,
            resolution=resolution, n_samples=n_samples, znear=znear, zfar=zfar, depth_eps=depth_eps
        )
    else:
        # Still need a depth render for the silhouette even with no edges to test.
        depth_raw, pose, cam = render_depth(mesh_welded, eye, center, up, half_span, half_span,
                                              resolution=resolution, znear=znear, zfar=zfar)
        depth_true = pyrender_depth_to_true_distance(depth_raw, znear, zfar)
        segs = []

    mask = (depth_true < np.inf).astype(float)

    if contour_supersample and contour_supersample > 1:
        # The on-screen mask is only as fine as `resolution` (e.g. 1800px),
        # so marching squares over it produces a genuinely blocky polyline
        # on shallow-angle curves (e.g. a roofline) -- no amount of
        # after-the-fact smoothing fixes that, it just rounds the blocks
        # off. Instead, render a second depth-only pass at N times the
        # resolution purely to get a finer mask to run find_contours on,
        # then scale the resulting coordinates back down to output pixel
        # space. This is a depth-only render (cheap relative to full
        # shading/AO passes) and finds the silhouette's real shape instead
        # of manufacturing smoothness that isn't there.
        hi_res = int(resolution * contour_supersample)
        hi_depth_raw, _, _ = render_depth(
            mesh_welded, eye, center, up, half_span, half_span,
            resolution=hi_res, znear=znear, zfar=zfar
        )
        hi_depth_true = pyrender_depth_to_true_distance(hi_depth_raw, znear, zfar)
        hi_mask = (hi_depth_true < np.inf).astype(float)
        min_len = 25 * contour_supersample
        outer_contours = [c / contour_supersample
                           for c in measure.find_contours(hi_mask, 0.5)
                           if len(c) > min_len]
    else:
        outer_contours = [c for c in measure.find_contours(mask, 0.5) if len(c) > 25]

    if smooth_contours:
        outer_contours = [_smooth_closed_contour(c, iterations=smooth_iterations)
                           for c in outer_contours]

    ao_raster = None  # (gray uint8 array, alpha bool mask) or None if AO not requested
    ssao_raw = None   # (raw_ao float64 array, alpha bool mask) -- "ssao" mode only, finalized
                       # later by finalize_ssao_views() once every view's raw occlusion is in,
                       # so the contrast remap (HANDOVER_1-21.md item 2) uses one shared range
                       # across the whole render instead of stretching each view alone.
    if ao_mode == "ssao":
        ssao_raw = _compute_ssao_occlusion(depth_true, half_span, resolution)
    elif ao_mesh is not None:
        ao_raster = _render_ao_raster(ao_mesh, eye, center, up, half_span, resolution, znear, zfar,
                                       blur_sigma_px=ao_blur_sigma_px)

    return {
        "outer_contours": outer_contours,
        "ao_raster": ao_raster,
        "ssao_raw": ssao_raw,
        "ao_max_darkness": ao_max_darkness,
        "ao_blur_sigma_px": ao_blur_sigma_px,
        "edge_segments": segs,
        "pose": pose, "xmag": half_span, "ymag": half_span, "resolution": resolution,
    }


def finalize_ssao_views(view_results, lo_pct=2.0, hi_pct=98.0):
    """Second pass for "ssao" mode: gather every view's raw occlusion
    (view_results[v]["ssao_raw"]) computed by render_view(), find the
    lo_pct/hi_pct percentile across all of them combined, and remap every
    view against that one shared range -- fills in view_results[v]["ao_raster"].
    No-op for views that don't have ssao_raw set (AO off, or "vertex" mode,
    which already has ao_raster set directly by render_view()).

    Doing this once across the whole render, rather than per view, is what
    keeps darkness comparable between views in the same composite sheet
    (HANDOVER_1-21.md item 2) -- e.g. the top and bottom view no longer
    each stretch their own contrast independently, which would hide any
    genuine brightness difference between them behind two different remaps.
    """
    ssao_views = {v: r for v, r in view_results.items() if r.get("ssao_raw") is not None}
    if not ssao_views:
        return

    all_vals = []
    for r in ssao_views.values():
        raw_ao, alpha = r["ssao_raw"]
        if alpha.any():
            all_vals.append(raw_ao[alpha])
    if not all_vals:
        lo, hi = 0.0, 1.0
    else:
        pooled = np.concatenate(all_vals)
        lo = float(np.percentile(pooled, lo_pct))
        hi = float(np.percentile(pooled, hi_pct))
        if hi <= lo:
            lo, hi = 0.0, 1.0

    for r in ssao_views.values():
        raw_ao, alpha = r["ssao_raw"]
        r["ao_raster"] = _ssao_raw_to_gray(
            raw_ao, alpha,
            ao_max_darkness=r["ao_max_darkness"],
            blur_sigma_px=r["ao_blur_sigma_px"],
            lo=lo, hi=hi,
        )


class AOPerformanceError(Exception):
    """Raised when AO is requested but the fast (embree) ray intersector
    isn't actually active. Without it, trimesh silently falls back to a
    pure-Python ray intersector that is 100-1000x slower (confirmed via
    direct timing during development) — running AO on that path is the
    exact problem already hit once (a ~5 minute render that still produced
    a poor-quality, banded result). Failing loudly here, with a clear fix,
    is better than silently reproducing that experience again.
    """
    pass


def _check_embree_active(mesh):
    ray_class_name = type(mesh.ray).__name__
    if "pyembree" not in type(mesh.ray).__module__:
        raise AOPerformanceError(
            "Ambient occlusion requires the 'embreex' package for fast ray "
            "casting, but it doesn't seem to be active (using "
            f"{ray_class_name} instead). Without it, AO would take several "
            "minutes and still look poor. Run 'python -m pip install -r "
            "requirements.txt' to install it, then restart the app."
        )


def _sample_ao_hemisphere(points, normals, cast_target, n_rays, max_distance,
                           offset, seed=0, progress_callback=None, points_per_chunk=20000):
    """Casts cosine-weighted hemisphere rays from each (point, normal) pair
    against cast_target and returns an (n_points,) occlusion array in
    [0, 1] (0 = fully open, 1 = fully occluded). This is the exact
    ray-casting core originally written inline inside
    compute_ambient_occlusion() (see that function's docstring for the
    full history of every fix baked into this: 80 rays, distance-limited
    hits, the Duff et al. branchless orthonormal basis, ground-plane
    contact shadows via cast_target) — pulled out unchanged so the new
    UV-space texel bake (compute_ambient_occlusion_uv, in uv_bake.py) can
    reuse the identical, already-verified sampling core instead of a
    second hand-copied version that could quietly drift out of sync with
    fixes made to one but not the other.

    points/normals: (n, 3) arrays — for the per-vertex path these are mesh
    vertices/vertex normals; for the per-texel path they're texel surface
    positions/normals from uv_bake.rasterize_uv().
    """
    rng = np.random.default_rng(seed)
    n_points = len(points)
    occlusion = np.zeros(n_points)

    for i in range(0, n_points, points_per_chunk):
        chunk_pts = points[i:i + points_per_chunk]
        chunk_normals = normals[i:i + points_per_chunk]
        n_chunk = len(chunk_pts)

        u1 = rng.random((n_chunk, n_rays))
        u2 = rng.random((n_chunk, n_rays))
        r = np.sqrt(u1)
        theta = 2 * np.pi * u2
        lx = r * np.cos(theta)
        ly = r * np.sin(theta)
        lz = np.sqrt(np.maximum(0, 1 - u1))

        nx, ny, nz = chunk_normals[:, 0], chunk_normals[:, 1], chunk_normals[:, 2]
        sign = np.where(nz >= 0, 1.0, -1.0)
        a = -1.0 / (sign + nz)
        b = nx * ny * a
        tangent = np.stack([1.0 + sign * nx * nx * a, sign * b, -sign * nx], axis=1)
        bitangent = np.stack([b, sign + ny * ny * a, -ny], axis=1)

        dirs = (lx[:, :, None] * tangent[:, None, :]
                + ly[:, :, None] * bitangent[:, None, :]
                + lz[:, :, None] * chunk_normals[:, None, :])

        origins_flat = np.repeat(chunk_pts, n_rays, axis=0) + np.repeat(chunk_normals, n_rays, axis=0) * offset
        dirs_flat = dirs.reshape(-1, 3)

        locations, index_ray, _ = cast_target.ray.intersects_location(
            origins_flat, dirs_flat, multiple_hits=False)
        hits = np.zeros(len(origins_flat), dtype=bool)
        if len(index_ray):
            dist = np.linalg.norm(locations - origins_flat[index_ray], axis=1)
            within = dist <= max_distance
            hits[index_ray[within]] = True
        hits_per_point = hits.reshape(n_chunk, n_rays).sum(axis=1)
        occlusion[i:i + n_chunk] = hits_per_point / n_rays

        if progress_callback:
            progress_callback(min(i + n_chunk, n_points), n_points)

    return occlusion


def compute_ambient_occlusion(mesh, n_rays=80, progress_callback=None,
                               axis_cfg=None, max_distance_frac=0.035,
                               blur_sigma_px=1.4, subdivide_max_edge_frac=0.008,
                               subdivide_max_vertex_multiple=6, ao_max_darkness=0.5):
    """Computes a real geometric ambient-occlusion value per vertex by
    casting rays into the hemisphere above each vertex and checking how
    many hit nearby geometry (true occlusion — darkens creases, panel
    gaps, and recesses near OTHER geometry, unlike the old fake Lambertian
    shading this originally replaced, which only responded to surface
    angle and produced unhelpful "wiggly lines" with no real occlusion
    happening there).

    ao_max_darkness (0..1, default 0.5, user-facing slider is 0-100%):
    scales the whole darkness curve rather than clamping it -- 0 = no
    darkening at all (every vertex stays full brightness); 1 = full
    curve, darkest points reach AO_DARKNESS_CEILING. See the formula
    right before `brightness` is computed, near the end of this function,
    for the exact shared convention (same across all three AO modes).

    QUALITY PASS (this round): compared directly against a real baked AO
    texture the user supplied from the source game asset (a proper offline
    UV-space bake). Ours looked "muddier" and noisier by comparison for
    three concrete, fixable reasons, addressed here:

    1. n_rays raised 24 -> 80. Cheap given embree's speed (see FIX note
       below); the old default under-sampled the hemisphere enough to be
       visibly grainy next to a proper bake.
    2. Ray hits are now distance-limited (`max_distance_frac` of bbox_diag,
       default 3.5%). Previously ANY hit at ANY distance counted as full
       occlusion, so a ray grazing geometry clear across the car darkened
       a point exactly as much as a hit 2mm away — this was the single
       biggest source of the "muddy/global" look vs. the crisp, localized
       shading in the reference bake, which only lets nearby occluders
       contribute.
    3. A synthetic, occlusion-only ground plane is added beneath the mesh
       (requires `axis_cfg` to know which axis is "up") to produce contact
       shadows at the tires/rockers/skirts, matching what a game-asset AO
       bake typically includes and this renderer previously had no way to
       produce (there was no floor in the scene at all). Purely a raycast
       occluder — never rendered, never colored, doesn't touch the visible
       mesh.

    A light post-blur (`blur_sigma_px`) is applied downstream in
    _render_ao_raster on the final grayscale image to smooth residual
    per-ray sampling noise, the same way an offline baker denoises —
    see that function's docstring.

    REWRITTEN after a real bug found via user testing: the first version
    of this function downsampled to ~11,000 sample points with only 4 rays
    each (a workaround for the pure-Python ray intersector being far too
    slow at full resolution). With only 4 rays, occlusion could only take
    5 distinct values (0/4 .. 4/4), which produced sharp banded contour
    rings instead of smooth shading — visually identical to a topographic
    map, exactly what the user reported ("looks nothing like AO"). Root
    cause was the quantization, not a cosmetic issue to tune around.

    FIX: install `embreex` (a fast, compiled ray-mesh intersection
    library — confirmed via direct timing test during this fix: 4000 rays
    went from 8.77s on the pure-Python fallback to 0.23s with embree, and
    trimesh automatically uses it for `mesh.ray` once installed, no other
    code changes needed for that part). This is fast enough to compute AO
    at FULL per-vertex resolution with many rays per vertex directly —
    measured: 351,543 vertices (Probox) x 32 rays = ~11.2 million rays in
    under 4 seconds. The voxel-downsampling + nearest-neighbor-propagation
    workaround from the first version is no longer needed and has been
    removed entirely, eliminating the quantization artifact at its root
    rather than increasing ray count within the old downsampled approach
    (which would still have looked banded, just with more, smaller bands).

    n_rays=24 default chosen for visibly smooth gradation (25 distinct
    levels) while staying well within a fast, comfortable time budget now
    that embree is in use.

    `embreex` MUST be installed for this to be fast — added to
    requirements.txt and the app.py dependency self-check. If it is
    somehow missing despite that, trimesh silently falls back to its
    pure-Python ray intersector, which would make this function very slow
    again (the original problem) without an explicit error — this is a
    known soft spot, not yet defended against with an explicit check (see
    STATUS.md).

    SEAM-NORMAL FIX (this round): even after the speed/quantization/raster
    fixes above, real user files (Liana, ATCC touring car) still showed a
    streaky, "smudgy" look on otherwise flat painted body panels. Measured
    directly on the Liana's front-left door, restricted to a region with
    no real geometric detail (no handle, no badge, no crease): vertices
    within 2mm of each other — essentially the same physical point on a
    flat panel — had vertex normals 24-55 degrees apart, and computed AO
    brightness differing by up to 95/255. This is a genuine property of
    the source mesh, not a bug in this function: many real-time-engine
    assets (confirmed on both KN5 files tested) deliberately split/duplicate
    vertices along UV-island or smoothing-group seams with hard (non-
    averaged) normals on each side — invisible in the original game
    renderer's lighting model, but directly visible here because AO ray
    direction is sampled from the hemisphere around each vertex's own
    normal, so a hard seam in the MIDDLE of a visually flat panel produces
    a real, large discontinuity in ray sampling direction, and therefore
    in computed occlusion, right at the seam. A scan across the whole
    Liana mesh found 41,605 vertex clusters (by near-coincident position)
    with multiple disagreeing normals — this is widespread, not a one-off.

    FIX: before ray-casting, merge vertices that are near-coincident in
    POSITION (within `merge_tol`, tuned below) into clusters and assign
    each cluster a single averaged, re-normalized normal, used only for
    AO sampling — this does not alter mesh.vertices, faces, or the visual
    shading normals used anywhere else, only the hemisphere orientation
    used internally by this function. `merge_tol` was tuned empirically
    against the Liana's actual nearest-neighbour vertex distance
    distribution: over half of all vertices already have an exact (0.0
    distance) duplicate (confirming hard seam-splitting is the norm, not
    rare), pair count is nearly flat from 0.1mm-0.5mm (77.4k -> 79.8k
    pairs), then grows sharply past 1mm (79.8k -> 106k -> 209k at 1mm/2mm)
    as the radius starts catching genuinely distinct nearby detail instead
    of true seam duplicates. 0.25mm sits in the flat plateau, comfortably
    past real seam duplicates and well short of where false merges start.
    Expressed as a fraction of bbox_diag so it scales sensibly across
    differently-sized models rather than being a fixed-mm constant.

    Returns a new trimesh.Trimesh with AO baked in as grayscale vertex
    colors (darker = more occluded), ready to pass as `ao_mesh` to
    render_view(). NOTE: as of the adaptive-subdivision pass below, this
    is NOT guaranteed to have the same vertex/face count as the input
    `mesh` — see ADAPTIVE SUBDIVISION docstring section. That's fine: the
    returned mesh is used ONLY by _render_ao_raster() for the AO shading
    raster; the visible linework (silhouette/feature edges) is computed
    separately from the original mesh and never touches this one.

    ADAPTIVE SUBDIVISION (this round — the deferred item from the previous
    quality pass): low-triangle-density regions (e.g. the Liana's door
    glass, 161 faces spanning a large area) still looked blocky/faceted
    even after the distance-limiting, ray-count, and ground-plane fixes
    above. Root cause, confirmed by re-reading the render path: AO values
    are computed per-vertex, then pyrender's rasterizer linearly
    interpolates vertex colors across each triangle when drawing the AO
    raster. A handful of huge, sparse triangles means occlusion is
    sampled at only a few widely-spaced points and then crudely
    straight-line-interpolated across a large area — it isn't that the
    occlusion values themselves were wrong, it's that there weren't
    enough of them, spatially, for the interpolation to look smooth. More
    sample points across the SAME geometry fixes this at the source
    rather than trying to blur it away after the fact (a blur can hide
    noise, but can't invent gradient detail that was never sampled).

    FIX: before ray-casting, adaptively subdivide any triangle whose edge
    exceeds `subdivide_max_edge_frac` of bbox_diag (default 0.8%) using
    trimesh.remesh.subdivide_to_size — genuinely adaptive, not a uniform
    subdivide: edges already shorter than the threshold are left
    untouched, so this only adds vertices where the source mesh is
    actually sparse (confirmed: a 2-triangle test panel goes from 4 to
    ~386 vertices at a 0.4%-of-bbox_diag threshold, while an
    already-fine region of the same mesh gains nothing). subdivide_to_size
    returns an unwelded "triangle soup"; rebuilding it as a processed
    trimesh.Trimesh welds any exactly-coincident points it introduces
    back together (it does NOT touch pre-existing UV-seam duplicates from
    the source mesh — those still go through the existing seam-normal
    merge step below exactly as before, since new subdivision points
    along a seam are generated independently on each side, same as the
    original geometry was).

    A safety cap (`subdivide_max_vertex_multiple`, default 6x the input
    vertex count, hard ceiling 1.5M) guards against runaway subdivision
    on a pathological mesh (e.g. one enormous triangle spanning the whole
    model) — if the result would exceed the cap, subdivision is skipped
    entirely and this function falls back to the original mesh, exactly
    as before this round. Rather not risk a hang on a real file than
    guarantee smoothness on every possible input.
    """
    m_orig = mesh.copy()
    _check_embree_active(m_orig)
    m_orig.fix_normals()
    bbox_diag = np.linalg.norm(m_orig.bounds[1] - m_orig.bounds[0])
    if bbox_diag <= 0:
        bbox_diag = 1.0

    m = m_orig
    if subdivide_max_edge_frac and subdivide_max_edge_frac > 0:
        max_edge = bbox_diag * subdivide_max_edge_frac
        try:
            v2, f2 = trimesh.remesh.subdivide_to_size(
                m_orig.vertices, m_orig.faces, max_edge=max_edge, max_iter=8)
            # Cap is intentionally generous for genuinely low-poly meshes:
            # a pure multiple of the ORIGINAL vertex count (e.g. 6x) would
            # defeat the whole point here, since the meshes that most need
            # subdivision (a handful of huge sparse triangles) have very
            # few original vertices to multiply from. Use an absolute
            # floor so small/sparse meshes still get real headroom, a
            # multiple for already-moderate meshes, and a hard ceiling so
            # a genuinely enormous or already-dense mesh can't runaway.
            vertex_cap = min(
                1_500_000,
                max(400_000, len(m_orig.vertices) * subdivide_max_vertex_multiple),
            )
            if len(v2) <= vertex_cap:
                candidate = trimesh.Trimesh(vertices=v2, faces=f2, process=True)
                if len(candidate.vertices) > 0 and len(candidate.faces) > 0:
                    candidate.fix_normals()
                    m = candidate
            # else: silently skip subdivision, m stays m_orig -- see cap note above.
        except Exception:
            # Subdivision is a quality improvement, not a correctness
            # requirement -- if it fails on some pathological input for
            # any reason, fall back to the original mesh rather than
            # breaking AO entirely.
            m = m_orig

    pts = m.vertices
    raw_normals = m.vertex_normals.copy()

    # GROUND PLANE (quality-pass addition): an occlusion-only quad placed
    # just under the mesh's lowest point along axis_cfg.up_axis, so rays
    # cast downward from the underbody/tires/rocker-panel area have
    # something to hit -- producing contact-shadow darkening the same way
    # a real AO bake gets it from a floor in the bake scene. Without
    # axis_cfg we don't know which axis is "up", so this is skipped (falls
    # back to old no-floor behaviour) rather than guessing wrong.
    #
    # This plane is ONLY added to a separate ray-cast target (`cast_target`
    # below) used for intersects_location — it is never merged into `m`,
    # never colored, never rendered, and has no effect on vertex count,
    # indices, or anything returned to the caller.
    cast_target = m
    if axis_cfg is not None:
        up_idx = axis_cfg.axis_index(axis_cfg.up_axis)
        lo = m.bounds[0]
        hi = m.bounds[1]
        pad = bbox_diag * 0.15
        plane_y = lo[up_idx] - bbox_diag * 0.001  # hair below the lowest point
        other_idx = [i for i in range(3) if i != up_idx]
        corner = np.zeros((4, 3))
        signs = [(-1, -1), (-1, 1), (1, 1), (1, -1)]
        for k, (sa, sb) in enumerate(signs):
            corner[k, up_idx] = plane_y
            corner[k, other_idx[0]] = (lo[other_idx[0]] - pad) if sa < 0 else (hi[other_idx[0]] + pad)
            corner[k, other_idx[1]] = (lo[other_idx[1]] - pad) if sb < 0 else (hi[other_idx[1]] + pad)
        plane_faces = np.array([[0, 1, 2], [0, 2, 3]])
        ground = trimesh.Trimesh(vertices=corner, faces=plane_faces, process=False)
        cast_target = trimesh.util.concatenate([m, ground])
        _check_embree_active(cast_target)

    # Degenerate/zero-length vertex normals are a REAL problem on fragmented
    # meshes (confirmed: ~30% of vertices on the Holden VY KN5-derived mesh
    # have a near-zero normal, likely from degenerate slivers in that
    # mesh's heavy fragmentation — see STATUS.md). A zero normal breaks the
    # hemisphere-sampling basis construction below (cross product of a zero
    # vector is zero, causing a divide-by-zero that NaNs out and crashes
    # the ray intersector with "Coordinates must not have minimums more
    # than maximums"). Fix: replace any degenerate normal with a
    # reasonable fallback before any basis construction happens.
    normal_lengths = np.linalg.norm(raw_normals, axis=1)
    degenerate = normal_lengths < 0.5
    if degenerate.any():
        raw_normals[degenerate] = np.array([0.0, 0.0, 1.0])
        normal_lengths = np.linalg.norm(raw_normals, axis=1)
    raw_normals = raw_normals / normal_lengths[:, None]

    # Merge near-coincident vertices (true seam/UV-split duplicates) and
    # average their normals -- see SEAM-NORMAL FIX docstring above.
    merge_tol = bbox_diag * 0.00005  # ~0.25mm on a ~5m-bbox-diagonal car
    tree = cKDTree(pts)
    pairs = tree.query_pairs(r=merge_tol)
    n_verts = len(pts)
    if pairs:
        parent = np.arange(n_verts)

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]  # path halving
                x = parent[x]
            return x

        for i, j in pairs:
            ri, rj = _find(i), _find(j)
            if ri != rj:
                parent[ri] = rj
        roots = np.array([_find(i) for i in range(n_verts)])
        unique_roots, inverse = np.unique(roots, return_inverse=True)
        sums = np.zeros((len(unique_roots), 3))
        np.add.at(sums, inverse, raw_normals)
        counts = np.bincount(inverse, minlength=len(unique_roots)).astype(float)
        avg = sums / counts[:, None]
        avg_lens = np.maximum(np.linalg.norm(avg, axis=1, keepdims=True), 1e-8)
        avg /= avg_lens
        normals = avg[inverse]
    else:
        normals = raw_normals

    occlusion = _sample_ao_hemisphere(
        pts, normals, cast_target, n_rays=n_rays, max_distance=bbox_diag * max_distance_frac,
        offset=bbox_diag * 0.0005, seed=0, progress_callback=progress_callback,
    )

    # Convert occlusion (0=fully open, 1=fully occluded) to a brightness
    # value, same convention the old shading used (darker = more
    # enclosed). ao_max_darkness (0..1, user-facing slider is 0-100%) is a
    # multiplicative SCALE on the darkness curve, not a clamp/floor: the
    # whole curve compresses toward full brightness as the slider comes
    # down, rather than distinct occlusion values getting clipped to the
    # same output once they cross some threshold (that clamp behavior was
    # the actual bug reported -- see compute_directional_shading for the
    # concrete repro). AO_DARKNESS_CEILING caps how dark the fully-
    # occluded end can ever get, even at slider=100%, so full-max never
    # fully obscures the line art underneath. This exact formula --
    # AO_DARKNESS_CEILING * s * occlusion_term -- is shared verbatim
    # across all three AO modes (this function, compute_directional_shading,
    # _ssao_raw_to_gray) so the darkest value and the midpoint value line
    # up across modes at any given slider position.
    s = np.clip(ao_max_darkness, 0.0, 1.0)
    darkness = AO_DARKNESS_CEILING * s * occlusion
    brightness = 1.0 - darkness
    colors = np.stack([brightness, brightness, brightness, np.ones_like(brightness)], axis=1)
    m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=(colors * 255).astype(np.uint8))
    return m


def compute_directional_shading(mesh, axis_cfg=None, ao_max_darkness=0.5, gamma=1.6):
    """Cheap per-vertex shading based purely on surface-normal angle
    relative to a single fixed "up" direction (axis_cfg.up_vec) — no ray
    casting, no occlusion, no per-view recompute. A panel facing straight
    up reads brightest; the more a panel's normal tilts away from up, the
    darker it reads; anything facing sideways or down clamps to the same
    darkness floor as a straight-down panel. Baked once per mesh and reused
    across every view, same as compute_ambient_occlusion()'s vertex bake,
    so the "light direction" never changes between views -- unlike SSAO,
    which is computed fresh from each view's own screen-space depth buffer
    and therefore implicitly "shades" from whatever direction that view
    happens to be looking (requested: same light angle in every render).

    This is intentionally NOT occlusion -- it has no idea two panels are
    near each other, so it won't darken creases or panel gaps the way
    vertex/SSAO AO do. It only encodes "which way is this panel facing."
    That's the whole point here: a flat, predictable, direction-locked
    falloff instead of a recomputed-per-view occlusion look.

    gamma: power curve applied to the normalized dot product before the
    darkness floor. >1 pushes mid-tilt panels darker faster (falloff feels
    steeper near the top); 1.0 = plain linear falloff. 1.6 chosen as a
    reasonable middle ground so near-vertical panels (doors) read clearly
    darker than the roof without needing every single degree of tilt to
    matter.

    ao_max_darkness (0..1, user-facing slider is 0-100%): same
    curve-scale convention as compute_ambient_occlusion() and
    _ssao_raw_to_gray() -- see AO_DARKNESS_CEILING's module-level
    comment. Darkest point (dot=-1, straight down) and midpoint (dot=0,
    sideways) line up with the other two AO modes at any given slider
    position.

    Returns a new trimesh.Trimesh with shading baked in as grayscale vertex
    colors, ready to pass as `ao_mesh` to render_view() as a drop-in
    alternative to compute_ambient_occlusion()'s output -- same format,
    same downstream rendering path (_render_ao_raster).
    """
    m = mesh.copy()
    # multibody=True: this mesh is assembled from many disconnected body
    # parts (separate body panels/floor pan/aero pieces merged into one
    # trimesh, typical of these car assets), not one watertight shell.
    # Plain fix_normals() treats the WHOLE mesh as a single body for its
    # consistency check, which is undefined/unreliable across disconnected
    # islands -- confirmed as the cause of a real bug: the underside floor
    # pan came out with an inward-flipped normal, so it read as
    # near-sideways brightness instead of near-black despite facing
    # straight down. multibody=True fixes each connected component's
    # normals independently using its own signed-volume test, which is
    # the correct mode for this asset shape. (compute_ambient_occlusion()
    # has the same latent risk with its own plain fix_normals() call, but
    # ray-cast occlusion is far less sensitive to a flipped normal than a
    # direct dot-product read is, so it wasn't visibly broken there and
    # is left alone here rather than changing more than what was asked.)
    m.fix_normals(multibody=True)
    up = axis_cfg.up_vec if axis_cfg is not None else np.array([0.0, 1.0, 0.0])
    up = up / max(np.linalg.norm(up), 1e-8)

    normals = m.vertex_normals.copy()
    lens = np.linalg.norm(normals, axis=1)
    degenerate = lens < 0.5
    if degenerate.any():
        normals[degenerate] = up
        lens = np.linalg.norm(normals, axis=1)
    normals = normals / lens[:, None]

    # dot=1 (facing straight up) -> brightest. dot=0 (facing sideways) ->
    # midpoint. dot=-1 (facing straight down) -> darkest. Full -1..1 range
    # is used (180 degrees of tilt) rather than clipping at 0, so sideways
    # and downward-facing panels are no longer treated identically -- the
    # darkness floor is now only hit at dot=-1 (straight down), with
    # sideways panels sitting at the midpoint of the falloff.
    dot = np.clip(normals @ up, -1.0, 1.0)
    lit = ((dot + 1.0) / 2.0) ** max(gamma, 1e-3)

    # Scale, not clamp -- see AO_DARKNESS_CEILING's module-level comment.
    # This is the fix for the reported bug: the old floor/clamp collapsed
    # every dot product past the floor threshold (e.g. a 95°-tilted side
    # panel and a 180° straight-down underside) onto the same output
    # brightness. The scale formula below never clips, so distinct (1 -
    # lit) values stay distinct all the way down to dot=-1.
    s = np.clip(ao_max_darkness, 0.0, 1.0)
    darkness = AO_DARKNESS_CEILING * s * (1.0 - lit)
    brightness = 1.0 - darkness

    colors = np.stack([brightness, brightness, brightness, np.ones_like(brightness)], axis=1)
    m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=(colors * 255).astype(np.uint8))
    return m


def _compute_ssao_occlusion(depth_true, half_span, resolution,
                             radius_world_frac=0.035, bias_world_frac=0.004,
                             n_directions=8, n_rings=3, hipass_world_frac=0.35):
    """Screen-space ambient occlusion, raw occlusion pass only (no contrast
    remap, no darkness scaling, no blur/output conversion — see
    _ssao_raw_to_gray for that). Split out so callers can gather raw
    occlusion across every view in a render and remap them all against one
    shared range (see HANDOVER_1-21.md item 2) instead of each view
    stretching its own contrast independently.

    Computed directly from THIS view's own orthographic depth buffer — no
    mesh baking, no UVs, no ray casting, and genuinely per-view (recomputed
    for every camera angle, unlike the vertex-baked mode which reuses one
    geometry-only bake across all views).

    Because the camera is orthographic, a fixed number of screen pixels
    always corresponds to the same real-world distance everywhere in the
    image, so occlusion can be estimated directly from the depth buffer's
    own heightfield-like structure: for a ring of sample points around each
    pixel, a neighbor that sits noticeably closer to the camera (smaller
    depth) than the pixel itself acts like a nearby wall blocking ambient
    light, in proportion to how steep the "horizon angle" up to it is
    (steeper + closer = more occlusion). Background samples (nothing
    rendered there) contribute zero occlusion, since there's nothing there
    to block light. This is the same family of technique as classic SSAO
    (Crysis-style depth-buffer AO / heightfield horizon AO), simplified to
    the orthographic case where screen distance IS world distance up to one
    constant factor.

    Before sampling, the depth buffer is high-passed: a heavily blurred copy
    of itself (sigma set by hipass_world_frac, deliberately much wider than
    the sampling radius) is subtracted off, and the ring kernel reads the
    residual instead of raw depth. This throws away broad panel curvature —
    the roof dome, the hood's taper, the windshield rake — which otherwise
    reads as false occlusion on every curved panel purely from the panel's
    own shape, with no real occluding geometry nearby (see HANDOVER_1-21.md
    item 1). Local, sharp deviations — creases, panel gaps, wheel wells —
    survive the high-pass and still drive real occlusion.

    Returns (raw_ao float64 array in [0, 1], alpha bool mask).
    """
    alpha = depth_true < np.inf
    H, W = depth_true.shape
    if not alpha.any():
        return np.zeros((H, W), dtype=np.float64), alpha

    px_per_world = resolution / (2.0 * half_span)
    world_radius = radius_world_frac * (2.0 * half_span)
    bias = bias_world_frac * (2.0 * half_span)

    far_fill = depth_true[alpha].max() + world_radius * 10.0
    d_raw = np.where(alpha, depth_true, far_fill)

    # High-pass: subtract broad curvature/tilt, keep local structure only.
    hipass_world = hipass_world_frac * (2.0 * half_span)
    hipass_sigma_px = max(1.0, hipass_world * px_per_world)
    broad = gaussian_filter(d_raw, sigma=hipass_sigma_px)
    d = d_raw - broad

    occ_accum = np.zeros((H, W), dtype=np.float64)
    weight_accum = 0.0

    for ring in range(1, n_rings + 1):
        r_world = world_radius * (ring / n_rings)
        r_px = max(1, int(round(r_world * px_per_world)))
        # Nearer rings weighted more heavily -- a close occluder blocks more
        # of the ambient hemisphere than a distant one at the same angle.
        ring_weight = 1.0 / ring
        horiz_dist_world = r_px / px_per_world
        for k in range(n_directions):
            theta = 2 * np.pi * k / n_directions
            dx = int(round(r_px * np.cos(theta)))
            dy = int(round(r_px * np.sin(theta)))
            if dx == 0 and dy == 0:
                continue
            d_shift = np.roll(np.roll(d, dy, axis=0), dx, axis=1)
            alpha_shift = np.roll(np.roll(alpha, dy, axis=0), dx, axis=1)

            dz = np.maximum((d - d_shift) - bias, 0.0)
            contribution = dz / np.sqrt(dz * dz + horiz_dist_world * horiz_dist_world + 1e-9)
            contribution = np.where(alpha_shift, contribution, 0.0)

            occ_accum += contribution * ring_weight
            weight_accum += ring_weight

    raw_ao = np.clip(occ_accum / max(weight_accum, 1e-9), 0.0, 1.0)
    return raw_ao, alpha


def _ssao_raw_to_gray(raw_ao, alpha, ao_max_darkness=0.5, blur_sigma_px=1.4,
                       lo=0.0, hi=1.0):
    """Turn a raw occlusion array (from _compute_ssao_occlusion) into the
    final (gray uint8, alpha) raster, applying a contrast remap first
    (HANDOVER_1-21.md item 2): stretch [lo, hi] of the raw occlusion range
    to fill [0, 1] before darkness scaling. lo/hi should be the 2nd/98th
    percentile of raw occlusion -- computed once across every view in the
    render and passed in here the same for all of them, so darkness stays
    comparable between views in the same composite sheet instead of each
    view being stretched independently. Callers that want a per-view-only
    stretch can pass that view's own percentiles instead.

    ao_max_darkness (0..1, user-facing slider is 0-100%): same
    curve-scale convention as compute_ambient_occlusion() and
    compute_directional_shading() -- see AO_DARKNESS_CEILING's
    module-level comment.

    Returns (gray uint8 array, alpha bool mask), same format as
    _render_ao_raster.
    """
    H, W = raw_ao.shape
    if not alpha.any():
        return np.zeros((H, W), dtype=np.uint8), alpha

    span = max(hi - lo, 1e-6)
    stretched = np.clip((raw_ao - lo) / span, 0.0, 1.0)

    # Scale, not clamp -- see AO_DARKNESS_CEILING's module-level comment.
    # Same formula as compute_ambient_occlusion()/compute_directional_
    # shading() so darkest value and midpoint line up across all three
    # AO modes at any given slider position.
    s = np.clip(ao_max_darkness, 0.0, 1.0)
    darkness = AO_DARKNESS_CEILING * s * stretched
    gray = np.clip(255.0 * (1.0 - darkness), 0, 255)

    if blur_sigma_px and blur_sigma_px > 0:
        alpha_f = alpha.astype(np.float64)
        gray_f = gray * alpha_f
        blurred_gray = gaussian_filter(gray_f, sigma=blur_sigma_px)
        blurred_alpha = gaussian_filter(alpha_f, sigma=blur_sigma_px)
        safe_alpha = np.where(blurred_alpha > 1e-6, blurred_alpha, 1.0)
        denoised = blurred_gray / safe_alpha
        gray = np.where(alpha, np.clip(denoised, 0, 255), gray)

    return gray.astype(np.uint8), alpha


def _render_ao_raster(ao_mesh, eye, center, up, half_span, resolution, znear, zfar,
                       blur_sigma_px=1.4):
    """Renders the AO-baked mesh and returns the raw grayscale brightness
    image (+ alpha mask), for use as a multiply-blended raster underlay in
    the final composite — NOT contour lines.

    DENOISE PASS (quality-pass addition): a real offline AO bake denoises
    after raycasting; ours previously didn't, so residual per-ray sampling
    noise (visible even with 80 rays) showed through directly. A small
    Gaussian blur on the grayscale image closes most of that gap cheaply.
    It's done ALPHA-AWARE (blur the alpha-masked image and the alpha mask
    separately, then divide back out) specifically so background
    (alpha=0) pixels don't bleed grey into the silhouette edge — a plain
    blur across a hard alpha cutout would visibly soften/dim the model's
    outline, which would fight the crisp linework this app is built
    around.

    REPLACES the previous contour-line approach entirely. User feedback,
    confirmed by inspecting the actual delivered output: the contour-line
    version drew ONLY the boundaries between AO brightness levels (literal
    iso-lines, like a topographic map), with the shaded regions between
    those lines left blank. That was never going to read as a gradient no
    matter how many ray/level/material parameters got tuned, because the
    actual shaded pixel data was being discarded after extraction — only
    the contour boundaries were ever passed to the compositor. This was a
    pipeline design gap (no raster ever reached compose_image), not a
    rendering-quality bug, and tuning AO inputs further could never have
    fixed it.

    The PBR-vs-FLAT render fix from the previous round still applies and
    is kept here (FLAT bypasses pyrender's lighting pipeline so the baked
    vertex-color AO values pass straight through undistorted).
    """
    scene = pyrender.Scene(bg_color=[0, 0, 0, 0], ambient_light=[1, 1, 1])
    scene.add(pyrender.Mesh.from_trimesh(ao_mesh, smooth=True))
    pose = make_camera_pose(eye, center, up)
    cam = pyrender.OrthographicCamera(xmag=half_span, ymag=half_span, znear=znear, zfar=zfar)
    scene.add_node(pyrender.Node(camera=cam, matrix=pose))
    r = pyrender.OffscreenRenderer(resolution, resolution)
    color, depth = r.render(scene, flags=pyrender.RenderFlags.FLAT)
    r.delete()

    gray = color[:, :, 0].astype(np.uint8)  # 0=fully occluded .. 255=fully open
    alpha = (depth > 0)

    if blur_sigma_px and blur_sigma_px > 0 and alpha.any():
        alpha_f = alpha.astype(np.float64)
        gray_f = gray.astype(np.float64) * alpha_f
        blurred_gray = gaussian_filter(gray_f, sigma=blur_sigma_px)
        blurred_alpha = gaussian_filter(alpha_f, sigma=blur_sigma_px)
        safe_alpha = np.where(blurred_alpha > 1e-6, blurred_alpha, 1.0)
        denoised = blurred_gray / safe_alpha
        gray = np.where(alpha, np.clip(denoised, 0, 255), gray).astype(np.uint8)

    return gray, alpha


def _render_ao_contours(ao_mesh, eye, center, up, half_span, resolution, znear, zfar,
                         ao_levels, min_contour_area):
    """Renders the AO-baked mesh and extracts iso-brightness contour lines.

    NOTE: superseded by _render_ao_raster() above for the main AO-shading
    path, kept here only in case contour-style AO is wanted again later as
    an alternative rendering mode. Not currently called by render_view().
    """


    gray = color[:, :, 0].astype(float) / 255.0
    mask = depth > 0
    if not mask.any():
        return []
    finite = gray[mask]
    levels = np.linspace(finite.min(), finite.max(), ao_levels)[1:-1]
    filled = np.where(mask, gray, -1)

    contours = []
    for lev in levels:
        for c in measure.find_contours(filled, lev):
            if len(c) < 5:
                continue
            w_c = c[:, 1].max() - c[:, 1].min()
            h_c = c[:, 0].max() - c[:, 0].min()
            if w_c * h_c > min_contour_area or max(w_c, h_c) > 40:
                contours.append(c)
    return contours


def rib_cut_fractions(n_cuts):
    """Returns the list of forward-axis fractions (0..1) at which
    render_rib_sections() above places its cuts, WITHOUT actually running
    the (relatively expensive) mesh-plane intersection — used by the
    compositor to draw a faint position-indicator line on the other views
    showing where each cut is taken from, without needing the real mesh
    section geometry for that purpose. Kept as a separate function rather
    than having render_rib_sections() also return fractions, so a caller
    that only needs the fractions (no rendering yet) doesn't have to wait
    on the real section computation. Logic must stay IDENTICAL to the
    fraction calculation inside render_rib_sections() above — same
    formula, both reference n_cuts the same way — since these two
    functions describing the same cuts diverging would silently draw
    indicator lines in the wrong place.
    """
    if n_cuts < 1:
        raise ValueError("n_cuts must be >= 1 (1 = midpoint cut, matching the rib section's prior behavior).")
    return [k / (n_cuts + 1) for k in range(1, n_cuts + 1)]


def render_rib_sections(mesh, axis_cfg, n_cuts=1):
    """Width-wise cross-section(s) perpendicular to the model's forward
    axis. True geometric mesh-plane intersection (trimesh's mesh.section),
    independent of mesh topology quality.

    n_cuts divides the model's extent along the forward axis into
    (n_cuts + 1) EQUAL segments, with a cut plane at each internal
    boundary (exact spec, confirmed with user — not approximate):
        n_cuts=1 (minimum): one cut, at exactly the midpoint (1/2).
        n_cuts=2: cuts at 1/3 and 2/3.
        n_cuts=3: cuts at 1/4, 1/2, 3/4.
        general: cut k (1-indexed) sits at fraction k/(n_cuts+1) of the
        model's extent along the forward axis, measured from the minimum
        to the maximum extent on that axis (NOT from front_sign direction
        specifically — the fractions are purely spatial, front_sign only
        matters for labeling/ordering if ever needed, not for cut position).

    Returns a list of length n_cuts, each element a list of
    (p0_world, p1_world) segment pairs for that cut (some cuts may return
    an empty list if the plane happens to miss all geometry, e.g. a
    degenerate cut at the very tip — not expected with this spec since
    n_cuts=1 is forced to the midpoint and others are always interior
    fractions, but handled gracefully regardless).
    """
    if n_cuts < 1:
        raise ValueError("n_cuts must be >= 1 (1 = midpoint cut, matching the rib section's prior behavior).")

    fwd_idx = axis_cfg.axis_index(axis_cfg.forward_axis)
    bmin, bmax = mesh.bounds
    lo, hi = bmin[fwd_idx], bmax[fwd_idx]

    fwd_vec = np.zeros(3)
    fwd_vec[fwd_idx] = 1.0

    all_segments = []
    for k in range(1, n_cuts + 1):
        frac = k / (n_cuts + 1)
        pos = lo + frac * (hi - lo)
        plane_origin = mesh.bounds.mean(axis=0)  # any point; only the forward-axis component matters
        plane_origin[fwd_idx] = pos

        section = mesh.section(plane_origin=plane_origin, plane_normal=fwd_vec)
        segments = []
        if section is not None:
            for entity in section.entities:
                pts = section.vertices[entity.points]
                for i in range(len(pts) - 1):
                    segments.append((pts[i], pts[i + 1]))
        all_segments.append(segments)

    # all_segments is built lo->hi along the forward (raw) axis. The
    # "front" camera position is defined elsewhere as center - fwd_vec*dist
    # (see _view_geometry's "front" entry), where fwd_vec = axis * front_sign
    # -- so front sits on the NEGATIVE fwd_vec side, i.e. front is at the
    # LOW end of the raw axis when front_sign > 0, and at the HIGH end when
    # front_sign < 0 (the inverse of what you'd guess from the sign alone).
    # front_sign > 0 -> front = lo -> lo->hi already reads front-to-back,
    # no reversal needed. front_sign < 0 -> front = hi -> lo->hi reads
    # back-to-front and must be reversed. (Previous version of this had the
    # condition backwards, which silently no-op'd for negative-front_sign
    # models -- exactly the case that was reported as still broken.)
    if axis_cfg.front_sign < 0:
        all_segments = list(reversed(all_segments))

    return all_segments
