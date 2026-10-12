"""PNG frames of the scene graph.

frame()     the stitched map coloured by owner, anchors, leaf cells, links with cost / width.
pipeline()  four panels of one state, left to right: what the robot reported, what is navigable and how wide,
            the regions (mcmaps) and their seams, the anchor graph with the portals.
"""
import colorsys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

UNKNOWN_RGB = np.array([0.90, 0.90, 0.90])


def colour(i: int) -> np.ndarray:
    """A distinct colour per anchor id (golden-ratio hues)."""
    return np.array(colorsys.hsv_to_rgb((i * 0.61803398875) % 1.0, 0.55, 0.92))


def _owner_rgb(own: np.ndarray) -> np.ndarray:
    ids = np.unique(own[own >= 0])
    lut = {int(i): colour(int(i)) for i in ids}
    out = np.zeros(own.shape + (3,))
    for i, c in lut.items():
        out[own == i] = c
    return out


def _seams(own: np.ndarray) -> np.ndarray:
    a = own
    s = np.zeros(a.shape, bool)
    s[:, :-1] |= (a[:, :-1] != a[:, 1:]) & (a[:, :-1] >= 0) & (a[:, 1:] >= 0)
    s[:-1, :] |= (a[:-1, :] != a[1:, :]) & (a[:-1, :] >= 0) & (a[1:, :] >= 0)
    return s


def _crop(val):
    r, c = np.nonzero(val != 255)
    return c.min(), r.min(), c.max() + 1, r.max() + 1


def _canvas(sg):
    reg = sg.region()
    p = sg.p
    c0, r0, c1, r1 = _crop(reg.val)
    sl = (slice(r0, r1), slice(c0, c1))
    ox, oy = (reg.i0 + c0) * p.res, (reg.j0 + r0) * p.res
    ext = (ox, ox + (c1 - c0) * p.res, oy, oy + (r1 - r0) * p.res)
    return reg, sl, ext


def _decorate(ax, sg, ext, leaf_cells=(), current=None, links=None, labels=True, ids=True):
    p = sg.p
    if links:
        for L in links:
            a, b = sg.anchors.get(L.a), sg.anchors.get(L.b)
            if a is None or b is None:
                continue
            ax.plot([(a.ci + .5) * p.res, L.portal[0], (b.ci + .5) * p.res],
                    [(a.cj + .5) * p.res, L.portal[1], (b.cj + .5) * p.res], color="k", lw=1.1, alpha=.85)
            if labels:
                ax.annotate(f"{L.cost:.1f} / {L.width:.2f}m", L.portal, fontsize=6.5,
                            bbox=dict(boxstyle="round,pad=0.12", fc="w", ec="none", alpha=.85))
    if len(leaf_cells):
        lc = np.array(leaf_cells, float)
        ax.plot((lc[:, 0] + .5) * p.res, (lc[:, 1] + .5) * p.res, ".", ms=3, color="#0044cc")
    if current is not None:
        ax.plot((current[0] + .5) * p.res, (current[1] + .5) * p.res, "o", ms=11, mfc="none", mec="#0044cc", mew=2)
    for a in sg.anchors.values():
        ax.plot((a.ci + .5) * p.res, (a.cj + .5) * p.res, "o", ms=11, mfc="#ffd400", mec="k", mew=1.4)
        if ids:
            ax.annotate(str(a.id), ((a.ci + .5) * p.res, (a.cj + .5) * p.res), ha="center", va="center", fontsize=6.5)
    ax.set_aspect("equal")
    ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3])


def _regions_rgb(reg, sl):
    p = reg.p
    val, own = reg.val[sl], reg.own[sl]
    img = np.ones(val.shape + (3,)) * UNKNOWN_RGB
    known = val != 255
    free = known & (val < p.v_block)
    col = _owner_rgb(own)
    owned = own >= 0
    img[free & owned] = 0.45 * col[free & owned] + 0.55
    img[free & ~owned] = (0.97, 0.93, 0.70)               # free, nobody's: frontier
    img[known & ~free & owned] = 0.45 * col[known & ~free & owned]
    img[known & ~free & ~owned] = (0.15, 0.05, 0.05)
    return img, own


def frame(sg, path, title="", leaf_cells=(), current=None, links=None, note=""):
    reg, sl, ext = _canvas(sg)
    img, own = _regions_rgb(reg, sl)
    img[_seams(own)] = (0, 0, 0)
    fig, ax = plt.subplots(figsize=(9.5, 8))
    ax.imshow(img, origin="lower", extent=ext, interpolation="nearest")
    _decorate(ax, sg, ext, leaf_cells, current, links)
    ax.set_title(title, fontsize=10); ax.set_xlabel("x (m)"); ax.set_ylabel("y (m)")
    if note:
        fig.text(0.01, 0.01, note, fontsize=8, family="monospace", va="bottom")
    fig.tight_layout(rect=(0, 0.05 if note else 0, 1, 1))
    fig.savefig(path, dpi=100)
    plt.close(fig)


def pipeline(sg, path, title="", leaf_cells=(), links=None):
    reg, sl, ext = _canvas(sg)
    p = sg.p
    fig, axs = plt.subplots(1, 4, figsize=(26, 6.8))
    val = reg.val[sl]
    # 1 what the robot reported (stitched, newest wins)
    img = np.ones(val.shape + (3,)) * UNKNOWN_RGB
    known = val != 255
    soft = known & (val > 0) & (val < p.v_block)
    img[known & (val == 0)] = (1, 1, 1)
    img[soft] = np.stack([np.ones(soft.sum()), 1 - val[soft] / 150.0, 0.4 * np.ones(soft.sum())], 1)
    img[known & (val >= p.v_block)] = (0, 0, 0)
    axs[0].imshow(img, origin="lower", extent=ext, interpolation="nearest")
    axs[0].set_title("1  stitched costmap: free / soft / lethal / unknown (grey)")
    # 2 navigable and how wide
    clear = np.where(reg.f.nav[sl], reg.f.clear[sl], np.nan)
    im = axs[1].imshow(np.ma.masked_invalid(clear), origin="lower", extent=ext, cmap="viridis", interpolation="nearest")
    axs[1].set_facecolor((0.85, 0.85, 0.85))
    fig.colorbar(im, ax=axs[1], fraction=0.035, label="clearance (m)")
    axs[1].set_title("2  navigable cells (robot fits) and clearance")
    # 3 regions and seams
    img3, own = _regions_rgb(reg, sl)
    img3[_seams(own)] = (0, 0, 0)
    axs[2].imshow(img3, origin="lower", extent=ext, interpolation="nearest")
    axs[2].set_title("3  regions = mcmaps (black = seams)")
    # 4 graph
    axs[3].imshow(0.35 * img3 + 0.65, origin="lower", extent=ext, interpolation="nearest")
    axs[3].set_title("4  anchors, links (cost / width), leaves")
    for k, ax in enumerate(axs):
        _decorate(ax, sg, ext, leaf_cells if k == 3 else (), None, links if k == 3 else None, labels=(k == 3), ids=(k >= 2))
        ax.set_xlabel("x (m)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=80)
    plt.close(fig)
