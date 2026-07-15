"""
compositor.py — Composes rendered view data (from renderer.py) into a single
output image, using pixel-exact bounding-box layout (NOT matplotlib GridSpec,
which fights aspect-equal scaling across panels of different aspect ratios —
confirmed during earlier development).
"""

import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors
from matplotlib.collections import LineCollection

from visibility import project_points
from renderer import rib_cut_fractions


def _view_used_bbox(result, pad_frac=0.05):
    xs, ys = [], []
    for c in result["outer_contours"]:
        xs.extend(c[:, 1]); ys.extend(c[:, 0])
    if result.get("ao_raster") is not None:
        _, alpha = result["ao_raster"]
        rows = np.any(alpha, axis=1)
        cols = np.any(alpha, axis=0)
        if rows.any():
            y_idx = np.where(rows)[0]
            x_idx = np.where(cols)[0]
            ys.extend([y_idx.min(), y_idx.max()])
            xs.extend([x_idx.min(), x_idx.max()])
    pose, xmag, ymag, resolution = result["pose"], result["xmag"], result["ymag"], result["resolution"]
    for p0, p1 in result["edge_segments"]:
        pts = np.array([p0, p1])
        px, py, _ = project_points(pts, pose, xmag, ymag, resolution)
        xs.extend(px); ys.extend(py)
    if not xs:
        return (0, resolution, 0, resolution)
    xs = np.array(xs); ys = np.array(ys)
    w = xs.max() - xs.min(); h = ys.max() - ys.min()
    pad = pad_frac * max(w, h) if max(w, h) > 0 else 1
    return xs.min() - pad, xs.max() + pad, ys.min() - pad, ys.max() + pad


def _rib_used_bbox(rib_segments, ppm, h_idx, v_idx, pad_frac=0.05):
    if not rib_segments:
        return (0, 1, 0, 1)
    xs, ys = [], []
    for p0, p1 in rib_segments:
        xs += [p0[h_idx] * ppm, p1[h_idx] * ppm]
        ys += [-p0[v_idx] * ppm, -p1[v_idx] * ppm]
    xs = np.array(xs); ys = np.array(ys)
    w = xs.max() - xs.min(); h = ys.max() - ys.min()
    pad = pad_frac * max(w, h) if max(w, h) > 0 else 1
    return xs.min() - pad, xs.max() + pad, ys.min() - pad, ys.max() + pad


def _world_to_panel_x_frac(world_coord, ppm, bbox):
    """Converts a single world-space coordinate (already known to be
    along whichever axis is horizontal on this panel) into a 0..1
    fraction of the panel's own (already padded) bbox -- for placing a
    cross-reference marker showing where a *different* cut intersects
    this one. Exact, not an approximation: unlike the orthographic-view
    marker fracs (which reuse rib_cut_fractions() under the assumption
    that a view's content spans the mesh's full extent along that axis,
    true by construction for a full silhouette view), an individual
    rib/longitudinal panel's own content span can be narrower or
    off-center relative to the mesh's overall bounds, so this works
    directly off that panel's own already-computed bbox instead."""
    xmin, xmax = bbox[0], bbox[1]
    span = xmax - xmin
    if span == 0:
        return 0.5
    return (world_coord * ppm - xmin) / span


def _draw_part_labels(ax, part_labels, part_numbers, bbox, line_color, bg_color, cs=1.0):
    """Draws a small numbered marker (filled circle + number) at each
    part's projected position in this view. part_labels: {name: (px, py)}
    from renderer.project_part_labels(); part_numbers: {name: int}, the
    stable legend numbering assigned once in app.py (same numbers across
    every view/panel on the page).

    Positions outside this view's own bbox are skipped rather than
    clipped/clamped to the edge — a label for a part that isn't actually
    visible/framed in this particular panel (e.g. an interior part in a
    view where it's projected off to the side) shouldn't show up floating
    at the border.
    """
    xmin, xmax, ymin, ymax = bbox
    r = 0.011 * max(xmax - xmin, ymax - ymin)  # marker radius, scaled to this panel
    for name, (px, py) in part_labels.items():
        if not (xmin <= px <= xmax and ymin <= py <= ymax):
            continue
        num = part_numbers.get(name)
        if num is None:
            continue
        ax.add_patch(plt.Circle((px, py), r, facecolor=bg_color, edgecolor=line_color,
                                 linewidth=1.0, zorder=4))
        ax.text(px, py, str(num), ha="center", va="center", fontsize=8 * cs,
                family="DejaVu Sans", color=line_color, zorder=5)


def _draw_view(ax, result, bbox, line_color, bg_color="#FFFFFF",
                lw_outer=0.9, lw_edge=0.55, label=None,
                rib_marker_x_fracs=None, rib_marker_y_fracs=None,
                part_labels=None, part_numbers=None, cs=1.0, label_yfrac=-0.08):
    """Draws one view: AO shading as a raster underlay, tinted between the
    background colour and the line colour, then clean line art on top.

    label: optional view name ("FRONT", "LEFT", etc.) drawn beneath the
    view in line_color, small caps, DejaVu Sans (chosen over a system font
    like Gill Sans specifically because it ships bundled inside matplotlib
    itself — guaranteed present and rendering identically on every machine
    this app runs on, regardless of what's installed on that PC; Gill Sans
    is a commercial font not reliably present even on most Windows
    installs, and a missing-font silent fallback would make the look
    inconsistent across different users' output for no good reason).

    rib_marker_x_fracs / rib_marker_y_fracs: optional lists of fractional
    positions (0..1 across this view's own bbox) at which to draw a faint
    dashed line showing where a rib/cross-section cut is taken from.
    x_fracs draws a VERTICAL line (used on left/right, where the car's
    length runs horizontally in the image); y_fracs draws a HORIZONTAL
    line (used on top/bottom, where the car's length runs vertically in
    the image) — confirmed empirically by projecting known forward-axis
    bounds through each view's actual camera, not assumed. front/back
    views look directly ALONG the forward axis (a cut position projects
    to the same pixel regardless of where along that axis it actually
    sits), so there is no meaningful line to draw there; compose_image()
    simply never passes markers for those two views.
    """
    pose, xmag, ymag, resolution = result["pose"], result["xmag"], result["ymag"], result["resolution"]
    xmin, xmax, ymin, ymax = bbox

    if result.get("ao_raster") is not None:
        gray, alpha = result["ao_raster"]
        bg_rgb = np.array(matplotlib.colors.to_rgb(bg_color))
        line_rgb = np.array(matplotlib.colors.to_rgb(line_color))
        gray_norm = gray.astype(float) / 255.0  # 0..1, 1=fully open/bright, 0=fully occluded
        # Linear interpolation per channel: t=1 (open) -> bg_color,
        # t=0 (occluded) -> line_color. Equivalent to the old multiply
        # blend exactly when line_color is black (line_rgb=0 reduces this
        # to gray_norm * bg_rgb), so the default black-on-white look is
        # unchanged; any other line colour now tints correctly instead.
        rgb = gray_norm[:, :, None] * bg_rgb[None, None, :] + (1 - gray_norm[:, :, None]) * line_rgb[None, None, :]
        rgba = np.dstack([rgb, alpha.astype(float)])
        # Raster is in its own (resolution x resolution) pixel space, same
        # convention used elsewhere in this file: row 0 = top, col 0 = left.
        # imshow's default origin matches that, so plot directly with
        # extent mapping pixel index -> (x, y) in the same space the line
        # art below already uses (no flip needed here).
        ax.imshow(rgba, extent=(0, resolution, resolution, 0), interpolation="bilinear", zorder=0)

    if rib_marker_x_fracs or rib_marker_y_fracs:
        # Faint, thin dashed line spanning the bbox's full height (for an
        # x-fraction marker, used on left/right) or full width (for a
        # y-fraction marker, used on top/bottom) at each requested
        # position — drawn BELOW the line art (zorder=1, between the AO
        # raster at 0 and the line art at 2) so it never competes
        # visually with the actual model linework, and ABOVE the AO
        # raster so it stays visible over shaded regions too. front/back
        # views look ALONG the forward axis (confirmed empirically: a
        # rib cut's position along that axis projects to the exact same
        # pixel regardless of where along the axis it actually is), so a
        # cut position has no meaningful line to draw there at all —
        # callers simply don't pass markers for those views.
        for frac, num in (rib_marker_x_fracs or []):
            x = xmin + frac * (xmax - xmin)
            ax.plot([x, x], [ymin, ymax], linestyle=(0, (2, 4)), linewidth=0.5,
                    color=line_color, alpha=0.35, zorder=1, solid_capstyle="butt")
            if num is not None:
                ax.text(x, ymin + (ymax - ymin) * 0.02, f"S{num}", ha="center", va="top",
                        fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85, zorder=1)
        for frac, num in (rib_marker_y_fracs or []):
            y = ymin + frac * (ymax - ymin)
            ax.plot([xmin, xmax], [y, y], linestyle=(0, (2, 4)), linewidth=0.5,
                    color=line_color, alpha=0.35, zorder=1, solid_capstyle="butt")
            if num is not None:
                ax.text(xmin + (xmax - xmin) * 0.01, y, f"S{num}", ha="left", va="center",
                        fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85, zorder=1)

    outer_segs = [np.column_stack([c[:, 1], c[:, 0]]) for c in result["outer_contours"]]
    if outer_segs:
        ax.add_collection(LineCollection(outer_segs, colors=line_color, linewidths=lw_outer,
                                          capstyle="round", joinstyle="round", zorder=2,
                                          antialiased=True))

    edge_pts = result["edge_segments"]
    if edge_pts:
        # Project all segment endpoints in one batched call rather than per-segment.
        pts = np.array(edge_pts).reshape(-1, 3)  # (2*N, 3): p0,p1,p0,p1,...
        px, py, _ = project_points(pts, pose, xmag, ymag, resolution)
        px = px.reshape(-1, 2)
        py = py.reshape(-1, 2)
        edge_segs = np.stack([np.column_stack([px[i], py[i]]) for i in range(len(px))])
        ax.add_collection(LineCollection(edge_segs, colors=line_color, linewidths=lw_edge,
                                          capstyle="round", joinstyle="round", zorder=2,
                                          antialiased=True))

    if part_labels and part_numbers:
        _draw_part_labels(ax, part_labels, part_numbers, bbox, line_color, bg_color, cs=cs)

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymax, ymin)
    ax.set_aspect("equal")
    ax.axis("off")

    if label:
        # Placed just below the view's own bbox, in axes-fraction y (< 0,
        # since matplotlib axes fraction coords grow upward but this view's
        # y-axis is inverted for image-pixel convention above — using
        # transform=ax.transAxes with a small negative y keeps the label
        # glued to the bottom of this specific view regardless of its
        # height, rather than fighting the inverted data coordinates).
        ax.text(0.5, label_yfrac, label, transform=ax.transAxes, ha="center", va="top",
                fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85,
                clip_on=False)


def _draw_rib(ax, rib_segments, ppm, bbox, h_idx, v_idx, line_color, lw=1.0,
              marker_x_fracs=None, cs=1.0):
    """Same batching fix as _draw_view, applied to rib/cross-section
    segments — these can also number in the thousands on fragmented
    meshes and would hit the identical per-Line2D overhead otherwise.

    h_idx/v_idx: which world-axis index (0=x,1=y,2=z) of each 3D segment
    point to plot as horizontal/vertical. The two axes left over after a
    cut differ depending on which axis was cut (forward for rib sections,
    side for the longitudinal section), so this can't be hardcoded.

    marker_x_fracs: optional list of (frac, num) tuples — frac is 0..1
    across THIS panel's own (already padded) bbox at which to draw a faint
    vertical dashed line; num is the S-number label to draw next to it, or
    None for an unlabeled marker. Used to cross-reference where a *different*
    cut intersects this one (e.g. showing each rib cut's position on the
    longitudinal panel, and the longitudinal cut's position on each rib
    panel — the latter has nothing to number, hence num=None)."""
    if rib_segments:
        segs = np.array([[[p0[h_idx] * ppm, -p0[v_idx] * ppm], [p1[h_idx] * ppm, -p1[v_idx] * ppm]]
                          for p0, p1 in rib_segments])
        ax.add_collection(LineCollection(segs, colors=line_color, linewidths=lw,
                                          capstyle="round", joinstyle="round"))
    xmin, xmax, ymin, ymax = bbox
    for frac, num in (marker_x_fracs or []):
        x = xmin + frac * (xmax - xmin)
        ax.plot([x, x], [ymin, ymax], linestyle=(0, (2, 4)), linewidth=0.5,
                color=line_color, alpha=0.35, zorder=1, solid_capstyle="butt")
        if num is not None:
            ax.text(x, ymin + (ymax - ymin) * 0.02, f"S{num}", ha="center", va="top",
                    fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85, zorder=1)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymax, ymin)
    ax.set_aspect("equal")
    ax.axis("off")


RIB_MAX_PER_ROW = 2  # user-requested cap: keeps rib rows close to a
                      # square/rectangle rather than a long thin strip,
                      # minimizing wasted canvas space.

# Per-view mapping of "forward-axis fraction" (0=rear-most point, 1=front-
# most point, matching renderer.rib_cut_fractions()) to where that shows up
# in each view's own 2D pixel space, confirmed EMPIRICALLY (not assumed) by
# projecting the mesh's known forward-axis bounds through each view's real
# camera — see the renderer/compositor work log in STATUS.md for the actual
# numbers. "x" means a vertical line at that fraction across the width;
# "y" means a horizontal line at that fraction down the height. front/back
# look directly along the forward axis, so a cut position always projects
# to the same pixel there regardless of where it actually is along that
# axis — there is no meaningful line to draw on those two views, and they
# are simply absent from this map on purpose, not by oversight.
_RIB_MARKER_AXIS = {
    "left":   ("x", True),   # xmin=z_lo=rear; mirrored so S1 appears at front (high-Z side)
    "right":  ("x", False),  # xmin=z_hi=front; not mirrored so S1 appears at front (low-frac=xmin)
    "top":    ("y", False),  # ymin=z_hi=front; not mirrored so S1 appears at front
    "bottom": ("y", True),   # ymin=z_lo=rear;  mirrored so S1 appears at front
}

# Which views show a meaningful marker line for the longitudinal (side-axis)
# cut's position, and on which pixel axis. Mirror image of _RIB_MARKER_AXIS:
# left/right look directly along the side axis (no meaningful line, same
# reasoning as front/back for rib cuts), while front/back/top/bottom all
# have the side axis running along their own screen-horizontal. No mirroring
# or padding-remap needed here at all, unlike rib markers -- the
# longitudinal cut is always at the exact midpoint (frac 0.5), which maps to
# pixel-frac 0.5 regardless of mirroring or padding, so it's always just
# [0.5] on the x-axis for every view in this set.
_LONGITUDINAL_MARKER_VIEWS = {"front", "back", "top", "bottom"}

HEADER_H = 160   # px, title block height (doubled alongside title/subtitle
                 # font sizes below, so the block still comfortably frames
                 # the bigger text instead of just scaling the text in place)
FOOTER_H = 64    # px, watermark strip height (doubled alongside watermark
                 # font size)
BORDER_MARGIN = 48  # px, gap between content and the border rectangle
                     # (also doubled-ish vs the old 28 -- this is what
                     # keeps the now-larger title/watermark text clear of
                     # the border line, see `inset` below)
BORDER_LW = 1.4
LABEL_GAP_PX = 10  # px, fixed gap between a view/rib panel's bottom edge and
                   # its caption text. Deliberately NOT a fraction of that
                   # panel's own axes height -- a fixed fraction (e.g. -0.08
                   # in axes-fraction coords) gives a tiny, correct-looking
                   # gap for short panels (front/back) but blows up into a
                   # huge gap for tall ones (top/bottom span the car's full
                   # length vertically on the page), which is exactly what
                   # pushed those two labels down onto the row below. This
                   # constant is converted to a per-row axes-fraction value
                   # at the call site (divided by that row's view_row_h),
                   # so the on-page gap is the same fixed number of pixels
                   # under every panel regardless of how tall it is.


def compose_image(view_results, rib_sections, rib_ppm, output_path,
                   axis_cfg, longitudinal_segments=None,
                   bg_color="#FFFFFF", line_color="#000000",
                   scale_pct=100, dpi_base=250,
                   model_name=None, show_chrome=True, part_numbers=None,
                   label_sections=True):
    """view_results: dict of {view_name: render_view() result}, only for
    views the user actually requested.
    axis_cfg: the AxisConfig the mesh was rendered with. Required (no
    default) because it determines which world-axis indices of each rib
    cut's 3D segment points are the correct pair to plot as
    horizontal/vertical -- previously hardcoded to (0,1), which was only
    correct by coincidence for the default up_axis="y"/forward_axis="z"
    setup (see _rib_used_bbox/_draw_rib).
    rib_sections: list of rib cuts, each a list of (p0,p1) segment pairs
    (as returned by renderer.render_rib_sections) — empty list if no rib
    cuts requested. Rib cuts are laid out in a grid, wrapping at
    RIB_MAX_PER_ROW per row (rather than one ever-widening row), so a
    large cut count doesn't tank per-slice resolution on the page.
    rib_ppm: pixels-per-world-unit scale factor for rib cuts (must match
    the same scale used for the orthographic views, for true relative size).
    axis_cfg: the AxisConfig the mesh was rendered with. Required (no
    default) because rib_sections/longitudinal_segments are cut along
    different axes and this is what determines which world-axis indices
    of each 3D segment point are the correct pair to plot as
    horizontal/vertical for each — the two hardcoded to (0,1) before this
    was added, which was only correct by coincidence for the default
    up_axis="y"/forward_axis="z" config (see _rib_used_bbox/_draw_rib).
    longitudinal_segments: flat (p0,p1) segment list for the single
    midpoint cut perpendicular to the side axis (renderer.
    render_longitudinal_section), or None/empty if not requested.
    scale_pct: output resolution scale, 25-100 (matches UI slider). Note
    this same scale_pct is what app.py used to compute view_results'
    render resolution (base_res * scale_pct/100) BEFORE calling this
    function — so content is already smaller in canvas-pixel-units at low
    scale_pct. Every chrome constant below (header/footer height, all
    font sizes, margins, gaps, legend sizing) is scaled by that identical
    factor (`cs`), so headers/watermark/labels shrink right along with
    the content instead of staying a fixed size while content shrinks —
    which is what made text look oversized at low output scales before
    this was fixed. dpi is left at a flat dpi_base (NOT re-multiplied by
    scale here) because the content size shrink already happened once,
    upstream, via render_res; multiplying dpi by scale again on top of
    that was double-applying the scale factor (content came out shrunk
    quadratically in scale_pct while chrome only shrunk linearly, which
    is exactly why proportions drifted at low scale_pct).
    model_name: shown in the title block ("<model_name> — Made in Ortho
    0.12 by 6wheel"); if None, the title line is omitted entirely rather
    than showing a blank/placeholder name.
    show_chrome: master switch for the border/title/watermark/labels —
    all drawn in line_color so they always match whatever colour scheme
    is in use. Defaults on; exists as a single off-switch in case a future
    caller wants the bare image with none of this (e.g. an internal
    diagnostic render) without threading four separate booleans through.
    """
    cs = max(0.1, min(1.0, scale_pct / 100.0))  # chrome scale factor --
    # matches app.py's render_res = base_res * scale_pct/100 exactly, so
    # chrome and content shrink together and stay in the same proportion
    # to each other at every scale_pct, matching how it looks at 100%.

    bboxes = {}
    sizes = {}
    for name, result in view_results.items():
        bbox = _view_used_bbox(result)
        bboxes[name] = bbox
        sizes[name] = (bbox[1] - bbox[0], bbox[3] - bbox[2])

    GAP = 40 * cs
    LABEL_H = (52 * cs) if show_chrome else 0  # extra row height reserved for the view-name label
                                         # (doubled alongside the label font size)

    rib_h_idx = axis_cfg.axis_index(axis_cfg.side_axis)
    rib_v_idx = axis_cfg.axis_index(axis_cfg.up_axis)
    long_h_idx = axis_cfg.axis_index(axis_cfg.forward_axis)
    long_v_idx = axis_cfg.axis_index(axis_cfg.up_axis)

    rib_bboxes = []
    rib_sizes = []
    has_rib = bool(rib_sections)
    if has_rib:
        for segs in rib_sections:
            bbox = _rib_used_bbox(segs, rib_ppm, rib_h_idx, rib_v_idx)
            rib_bboxes.append(bbox)
            rib_sizes.append((bbox[1] - bbox[0], bbox[3] - bbox[2]))
    rib_fracs = rib_cut_fractions(len(rib_sections)) if has_rib else []

    has_longitudinal = bool(longitudinal_segments)
    if has_longitudinal:
        long_bbox = _rib_used_bbox(longitudinal_segments, rib_ppm, long_h_idx, long_v_idx)
        long_size = (long_bbox[1] - long_bbox[0], long_bbox[3] - long_bbox[2])

    # rows: list of dicts describing what to draw and where. Each row is
    # either a "views" row (one or more named orthographic views side by
    # side) or a "rib" row (one chunk of up to RIB_MAX_PER_ROW rib cuts).
    rows = []  # list of dicts: {kind, height, width, ...}

    has_front = "front" in view_results
    has_back = "back" in view_results
    if has_front or has_back:
        names = [n for n in ("front", "back") if n in view_results]
        w = sum(sizes[n][0] for n in names) + GAP * (len(names) - 1)
        h = max(sizes[n][1] for n in names) + LABEL_H
        rows.append({"kind": "views", "names": names, "height": h, "width": w})

    for simple in ["left", "right"]:
        if simple in view_results:
            w, h = sizes[simple]
            rows.append({"kind": "views", "names": [simple], "height": h + LABEL_H, "width": w})

    # Top/bottom share one row, side by side (previously two stacked
    # full-width rows) — user-requested, since stacking wasted vertical
    # canvas space when both views together are no wider than one alone.
    top_bottom_names = [n for n in ("top", "bottom") if n in view_results]
    if top_bottom_names:
        w = sum(sizes[n][0] for n in top_bottom_names) + GAP * (len(top_bottom_names) - 1)
        h = max(sizes[n][1] for n in top_bottom_names) + LABEL_H
        rows.append({"kind": "views", "names": top_bottom_names, "height": h, "width": w})

    if has_rib:
        # Chunk rib cuts into groups of at most RIB_MAX_PER_ROW, each
        # chunk becoming its own row, so e.g. 7 cuts -> a row of 4 then a
        # row of 3, instead of one row of 7 squeezed into the page width.
        # Placed LAST (user-requested) — previously sat between the
        # left/right row and the top/bottom row, which read oddly since
        # the cross-section slices aren't an orthographic view like the
        # rest; putting them at the bottom of the page reads as "detail
        # cuts, appended after the main views" instead of interrupting
        # the view sequence.
        for start in range(0, len(rib_sections), RIB_MAX_PER_ROW):
            chunk_idx = list(range(start, min(start + RIB_MAX_PER_ROW, len(rib_sections))))
            chunk_sizes = [rib_sizes[i] for i in chunk_idx]
            row_h = max(s[1] for s in chunk_sizes) + LABEL_H
            row_w = sum(s[0] for s in chunk_sizes) + GAP * (len(chunk_sizes) - 1)
            rows.append({"kind": "rib", "indices": chunk_idx, "height": row_h, "width": row_w})

    if has_longitudinal:
        # Same placement rationale as rib rows above: not an orthographic
        # view, appended after everything else including rib cuts.
        w, h = long_size
        rows.append({"kind": "longitudinal", "height": h + LABEL_H, "width": w})

    if not rows:
        raise ValueError("No views selected to render.")

    content_w = max(r["width"] for r in rows)
    content_h = sum(r["height"] for r in rows) + GAP * (len(rows) - 1)

    # Legend block: numbered part list, laid out in columns beneath every
    # other row (after rib cuts, right above the footer/watermark) — a
    # flat text block, not tied to any one view's axes, since the same
    # numbering applies across every panel on the page.
    import math
    legend_names = sorted(part_numbers.keys(), key=lambda n: part_numbers[n]) if part_numbers else []
    LEGEND_FONTSIZE = 13 * cs
    LEGEND_ROW_H = 24 * cs
    LEGEND_COL_W = 260 * cs
    legend_h = 0
    legend_cols = 1
    legend_rows_per_col = 0
    if legend_names:
        n = len(legend_names)
        legend_cols = min(4, max(1, math.ceil(n / 20)))
        legend_rows_per_col = math.ceil(n / legend_cols)
        legend_h = 40 * cs + legend_rows_per_col * LEGEND_ROW_H  # +40*cs for "PARTS" header
        content_w = max(content_w, legend_cols * LEGEND_COL_W)

    # Reserve extra canvas space around the actual content for the border,
    # title block, and watermark strip, all drawn in line_color so they
    # always match whatever colour scheme is active. When show_chrome is
    # off, all four of these collapse to 0 and canvas size exactly matches
    # the previous (pre-this-feature) behaviour.
    side_margin = (BORDER_MARGIN * cs) if show_chrome else 0
    header_h = (HEADER_H * cs) if (show_chrome and model_name) else ((BORDER_MARGIN * cs) if show_chrome else 0)
    footer_h = (FOOTER_H * cs) if show_chrome else 0

    canvas_w = content_w + side_margin * 2
    canvas_h = content_h + header_h + footer_h + side_margin + (legend_h + GAP if legend_h else 0)

    dpi = dpi_base

    fig = plt.figure(figsize=(canvas_w / 100, canvas_h / 100), dpi=dpi, facecolor=bg_color)

    def add_axes_px(x0, y0_top, wpx, hpx):
        x_frac = x0 / canvas_w
        w_frac = wpx / canvas_w
        h_frac = hpx / canvas_h
        y0_bottom = canvas_h - y0_top - hpx
        y_frac = y0_bottom / canvas_h
        ax = fig.add_axes([x_frac, y_frac, w_frac, h_frac])
        ax.set_facecolor(bg_color)
        return ax

    y_cursor = header_h
    for row in rows:
        row_h, row_w = row["height"], row["width"]
        x_cursor = side_margin + (content_w - row_w) / 2
        view_row_h = row_h - LABEL_H
        label_yfrac = -(LABEL_GAP_PX * cs) / view_row_h if view_row_h > 0 else -0.08
        if row["kind"] == "views":
            for name in row["names"]:
                w_v, h_v = sizes[name]
                ax = add_axes_px(x_cursor, y_cursor, w_v, view_row_h)
                axis_kind, mirrored = _RIB_MARKER_AXIS.get(name, (None, False))
                if axis_kind and rib_fracs:
                    padded = bboxes[name]
                    nb = _view_used_bbox(view_results[name], pad_frac=0.0)
                    content_span = (nb[1] - nb[0]) if axis_kind == "x" else (nb[3] - nb[2])
                    padded_span  = (padded[1] - padded[0]) if axis_kind == "x" else (padded[3] - padded[2])
                    n = len(rib_fracs)
                    fracs_for_view = [
                        (0.5 + ((1 - f if mirrored else f) - 0.5) * content_span / padded_span,
                         (n - i if axis_kind == "x" else i + 1) if (show_chrome and label_sections) else None)
                        for i, f in enumerate(rib_fracs)
                    ]
                else:
                    fracs_for_view = []

                x_fracs = fracs_for_view if axis_kind == "x" else []
                y_fracs = fracs_for_view if axis_kind == "y" else []
                if has_longitudinal and name in _LONGITUDINAL_MARKER_VIEWS:
                    x_fracs = x_fracs + [(0.5, None)]

                _draw_view(
                    ax, view_results[name], bboxes[name], line_color, bg_color,
                    label=(name.upper() if show_chrome else None),
                    rib_marker_x_fracs=(x_fracs or None),
                    rib_marker_y_fracs=(y_fracs or None),
                    part_labels=view_results[name].get("part_labels"),
                    part_numbers=part_numbers, cs=cs, label_yfrac=label_yfrac,
                )
                x_cursor += w_v + GAP
        elif row["kind"] == "rib":
            for n, i in enumerate(row["indices"]):
                w_r, h_r = rib_sizes[i]
                ax = add_axes_px(x_cursor, y_cursor, w_r, view_row_h)
                # Cross-reference marker: where the longitudinal cut (a
                # fixed world position along the side axis, same value for
                # every one of its segment points) falls on THIS rib panel.
                cross_fracs = []
                if has_longitudinal:
                    long_world_pos = longitudinal_segments[0][0][rib_h_idx]
                    cross_fracs = [(_world_to_panel_x_frac(long_world_pos, rib_ppm, rib_bboxes[i]), None)]
                _draw_rib(ax, rib_sections[i], rib_ppm, rib_bboxes[i], rib_h_idx, rib_v_idx, line_color,
                          marker_x_fracs=cross_fracs, cs=cs)
                if show_chrome:
                    ax.text(0.5, label_yfrac, f"SECTION {i + 1}", transform=ax.transAxes, ha="center", va="top",
                            fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85, clip_on=False)
                x_cursor += w_r + GAP
        elif row["kind"] == "longitudinal":
            w_r, h_r = long_size
            ax = add_axes_px(x_cursor, y_cursor, w_r, view_row_h)
            # Cross-reference markers: where each rib cut (a fixed world
            # position along the forward axis) falls on the longitudinal
            # panel. Cuts that missed all geometry (empty segment list)
            # have no meaningful position and are skipped.
            cross_fracs = [
                (_world_to_panel_x_frac(segs[0][0][long_h_idx], rib_ppm, long_bbox), i + 1)
                for i, segs in enumerate(rib_sections) if segs
            ]
            _draw_rib(ax, longitudinal_segments, rib_ppm, long_bbox, long_h_idx, long_v_idx, line_color,
                      marker_x_fracs=cross_fracs, cs=cs)
            if show_chrome:
                ax.text(0.5, label_yfrac, "LONGITUDINAL SECTION", transform=ax.transAxes, ha="center", va="top",
                        fontsize=18 * cs, family="DejaVu Sans", color=line_color, alpha=0.85, clip_on=False)
            x_cursor += w_r + GAP
        y_cursor += row_h + GAP

    if legend_names:
        legend_ax = add_axes_px(side_margin, y_cursor, content_w, legend_h)
        legend_ax.set_xlim(0, content_w)
        legend_ax.set_ylim(legend_h, 0)  # top-down, matching the rest of this file
        legend_ax.axis("off")
        legend_ax.text(0, 12 * cs, "PARTS", fontsize=16 * cs, family="DejaVu Sans",
                        weight="bold", color=line_color, alpha=0.85, va="top")
        col_w = content_w / legend_cols
        for i, name in enumerate(legend_names):
            col = i // legend_rows_per_col
            row = i % legend_rows_per_col
            x = col * col_w
            y = 40 * cs + row * LEGEND_ROW_H
            legend_ax.text(x, y, f"{part_numbers[name]}", fontsize=LEGEND_FONTSIZE,
                            family="DejaVu Sans", color=line_color, va="top", ha="left",
                            clip_on=False)
            legend_ax.text(x + 28 * cs, y, name, fontsize=LEGEND_FONTSIZE, family="DejaVu Sans",
                            color=line_color, alpha=0.85, va="top", ha="left", clip_on=False)
        y_cursor += legend_h + GAP

    if show_chrome:
        # Full-canvas overlay axes (0..1 in both directions, no aspect
        # lock) for the border rectangle, title block, and watermark —
        # kept as ONE separate axes on top of everything else (zorder
        # doesn't need to be fought per-element this way) rather than
        # trying to draw these inside any single view's own axes, which
        # are all individually positioned/sized and not meant to know
        # about the canvas as a whole.
        overlay = fig.add_axes([0, 0, 1, 1])
        overlay.set_xlim(0, canvas_w)
        overlay.set_ylim(canvas_h, 0)  # top-down, matching the rest of this file's pixel convention
        overlay.axis("off")
        overlay.patch.set_alpha(0)

        # Border: a rectangle inset by roughly 40% of the side margin from
        # each edge of the canvas, so it reads as a clean frame around
        # everything (title, views, watermark) rather than just around
        # the model content.
        inset = side_margin * 0.4
        overlay.add_patch(plt.Rectangle(
            (inset, inset), canvas_w - 2 * inset, canvas_h - 2 * inset,
            fill=False, edgecolor=line_color, linewidth=BORDER_LW * cs, zorder=5,
        ))

        if model_name:
            overlay.text(canvas_w / 2, header_h * 0.40, model_name,
                         ha="center", va="center", fontsize=40 * cs, family="DejaVu Sans",
                         color=line_color, zorder=5)
            overlay.text(canvas_w / 2, header_h * 0.72, "Made in Ortho 0.12 by 6wheel",
                         ha="center", va="center", fontsize=22 * cs, family="DejaVu Sans",
                         color=line_color, alpha=0.75, zorder=5)

        # Watermark text: GitHub link stays bottom-right (as before);
        # YouTube link added bottom-left, both offset by the same
        # `inset`-based padding so the now-chrome-scaled glyphs still
        # clear the border line at every scale_pct rather than crowding
        # right up against it.
        overlay.text(canvas_w - inset - 16 * cs, canvas_h - inset - 16 * cs,
                     "github.com/6wheel/Ortho",
                     ha="right", va="bottom", fontsize=16 * cs, family="DejaVu Sans",
                     color=line_color, alpha=0.55, zorder=5)
        overlay.text(inset + 16 * cs, canvas_h - inset - 16 * cs,
                     "youtube.com/@6wheel",
                     ha="left", va="bottom", fontsize=16 * cs, family="DejaVu Sans",
                     color=line_color, alpha=0.55, zorder=5)

    plt.savefig(output_path, dpi=dpi, facecolor=bg_color)
    plt.close(fig)
    return output_path


def export_split_views(view_results, output_dir, base_name, axis_cfg,
                       rib_sections=None, rib_ppm=1.0,
                       rib_y_center=None, rib_x_center=None, long_x_center=None,
                       longitudinal_segments=None,
                       bg_color="#FFFFFF", line_color="#000000",
                       scale_pct=100, dpi_base=250):
    """All images max_dim x max_dim, anchored on model world center.

    axis_cfg: required, same reasoning as compose_image() -- the rib/
    longitudinal cut panels project world coordinates onto pixel axes that
    depend on which axis was cut, so a caller can't be allowed to omit it
    and silently get the default-case (0, 1) indices.

    rib_x_center/rib_y_center/long_x_center: the mesh's true center,
    pre-scaled by rib_ppm, along the relevant world axis. Without these, a
    rib/longitudinal panel centers itself on its OWN cut content's bbox
    rather than the mesh's actual center -- fine on its own, but that self-
    centering point generally does NOT coincide with where the matching
    view image (front/back for rib panels, left/right for the longitudinal
    panel) centers itself, since a single-plane cut's own extent can differ
    from the full silhouette's envelope. Overlaying the two images (e.g. in
    an external editor, or as coplanar-ish reference planes) then shows a
    real, model-dependent offset between them even though each image is
    individually correct. Passing the shared true-center value here anchors
    every panel to the same reference point instead.
    """
    cs = max(0.1, min(1.0, scale_pct / 100.0))
    pad_frac = 0.05

    rib_h_idx = axis_cfg.axis_index(axis_cfg.side_axis)
    rib_v_idx = axis_cfg.axis_index(axis_cfg.up_axis)
    long_h_idx = axis_cfg.axis_index(axis_cfg.forward_axis)
    long_v_idx = axis_cfg.axis_index(axis_cfg.up_axis)

    bboxes = {name: _view_used_bbox(result, pad_frac=0.0)
              for name, result in view_results.items()}

    def _span(name, axis):
        if name not in bboxes:
            return 0.0
        b = bboxes[name]
        return (b[1] - b[0]) if axis == "x" else (b[3] - b[2])

    W = max(_span("front", "x"), _span("back", "x"), _span("top", "x"), _span("bottom", "x"), 1.0)
    H = max(_span("front", "y"), _span("back", "y"), _span("left", "y"), _span("right", "y"), 1.0)
    D = max(_span("left", "x"), _span("right", "x"), _span("top", "y"), _span("bottom", "y"), 1.0)

    pad = max(W, H, D) * pad_frac
    max_dim = max(W, H, D) + 2 * pad

    rib_fracs = rib_cut_fractions(len(rib_sections)) if rib_sections else []

    saved = {}
    for name, result in view_results.items():
        if name not in {"front", "back", "left", "right", "top", "bottom"}:
            continue
        bbox = bboxes[name]
        cx = (bbox[0] + bbox[1]) / 2
        cy = (bbox[2] + bbox[3]) / 2
        square_bbox = (cx - max_dim / 2, cx + max_dim / 2,
                       cy - max_dim / 2, cy + max_dim / 2)

        axis_kind, mirrored = _RIB_MARKER_AXIS.get(name, (None, False))
        if axis_kind and rib_fracs:
            content_span = (bboxes[name][1] - bboxes[name][0]) if axis_kind == "x" \
                else (bboxes[name][3] - bboxes[name][2])
            fracs_for_view = [(0.5 + ((1 - f if mirrored else f) - 0.5) * content_span / max_dim, i + 1)
                               for i, f in enumerate(rib_fracs)]
        else:
            fracs_for_view = []

        x_fracs = fracs_for_view if axis_kind == "x" else []
        y_fracs = fracs_for_view if axis_kind == "y" else []
        if longitudinal_segments and name in _LONGITUDINAL_MARKER_VIEWS:
            x_fracs = x_fracs + [(0.5, None)]

        fig = plt.figure(figsize=(max_dim / 100 * cs, max_dim / 100 * cs),
                         dpi=dpi_base, facecolor=bg_color)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor(bg_color)
        _draw_view(ax, result, square_bbox, line_color, bg_color,
                   rib_marker_x_fracs=(x_fracs or None),
                   rib_marker_y_fracs=(y_fracs or None))

        out_path = os.path.join(output_dir, f"{base_name}_{name}.png")
        plt.savefig(out_path, dpi=dpi_base, facecolor=bg_color)
        plt.close(fig)
        saved[name] = out_path

    if rib_sections:
        all_xs, all_ys = [], []
        for segs in rib_sections:
            for p0, p1 in segs:
                all_xs += [p0[rib_h_idx] * rib_ppm, p1[rib_h_idx] * rib_ppm]
                all_ys += [-p0[rib_v_idx] * rib_ppm, -p1[rib_v_idx] * rib_ppm]
        if all_xs:
            rib_cx = rib_x_center if rib_x_center is not None else (min(all_xs) + max(all_xs)) / 2
            rib_cy = rib_y_center if rib_y_center is not None \
                else (min(all_ys) + max(all_ys)) / 2
        else:
            rib_cx = rib_x_center if rib_x_center is not None else 0.0
            rib_cy = rib_y_center if rib_y_center is not None else 0.0

        section_bbox = (rib_cx - max_dim / 2, rib_cx + max_dim / 2,
                        rib_cy - max_dim / 2, rib_cy + max_dim / 2)

        for i, segs in enumerate(rib_sections):
            if not segs:
                continue
            num = i + 1
            xmin, xmax, ymin, ymax = section_bbox
            cross_fracs = []
            if longitudinal_segments:
                long_world_pos = longitudinal_segments[0][0][rib_h_idx]
                cross_fracs = [(_world_to_panel_x_frac(long_world_pos, rib_ppm, section_bbox), None)]
            fig = plt.figure(figsize=(max_dim / 100 * cs, max_dim / 100 * cs),
                             dpi=dpi_base, facecolor=bg_color)
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_facecolor(bg_color)
            _draw_rib(ax, segs, rib_ppm, section_bbox, rib_h_idx, rib_v_idx, line_color,
                      marker_x_fracs=cross_fracs, cs=cs)
            ax.text(xmin + (xmax - xmin) * 0.01, ymin + (ymax - ymin) * 0.02,
                    f"SECTION {num}", ha="left", va="top",
                    fontsize=20 * cs, family="DejaVu Sans", color=line_color, alpha=0.7, zorder=3)
            out_path = os.path.join(output_dir, f"{base_name}_section_{num}.png")
            plt.savefig(out_path, dpi=dpi_base, facecolor=bg_color)
            plt.close(fig)
            saved[f"section_{num}"] = out_path

    if longitudinal_segments:
        all_xs = [p[long_h_idx] * rib_ppm for p0, p1 in longitudinal_segments for p in (p0, p1)]
        all_ys = [-p[long_v_idx] * rib_ppm for p0, p1 in longitudinal_segments for p in (p0, p1)]
        long_cx = long_x_center if long_x_center is not None else (min(all_xs) + max(all_xs)) / 2
        long_cy = rib_y_center if rib_y_center is not None else (min(all_ys) + max(all_ys)) / 2
        long_bbox = (long_cx - max_dim / 2, long_cx + max_dim / 2,
                     long_cy - max_dim / 2, long_cy + max_dim / 2)
        xmin, xmax, ymin, ymax = long_bbox
        cross_fracs = [
            (_world_to_panel_x_frac(segs[0][0][long_h_idx], rib_ppm, long_bbox), i + 1)
            for i, segs in enumerate(rib_sections or []) if segs
        ]
        fig = plt.figure(figsize=(max_dim / 100 * cs, max_dim / 100 * cs),
                         dpi=dpi_base, facecolor=bg_color)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor(bg_color)
        _draw_rib(ax, longitudinal_segments, rib_ppm, long_bbox, long_h_idx, long_v_idx, line_color,
                  marker_x_fracs=cross_fracs, cs=cs)
        ax.text(xmin + (xmax - xmin) * 0.01, ymin + (ymax - ymin) * 0.02,
                "LONGITUDINAL SECTION", ha="left", va="top",
                fontsize=20 * cs, family="DejaVu Sans", color=line_color, alpha=0.7, zorder=3)
        out_path = os.path.join(output_dir, f"{base_name}_longitudinal.png")
        plt.savefig(out_path, dpi=dpi_base, facecolor=bg_color)
        plt.close(fig)
        saved["longitudinal"] = out_path

    layout = {"W": W, "H": H, "D": D, "max_dim": max_dim, "rib_fracs": rib_fracs}
    return saved, layout
