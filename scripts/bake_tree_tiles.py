#!/usr/bin/env python3
"""Bake the forest-trees style's crown height-field tiles (conifer + broadleaf) into tileable RGBA8 PNGs.

The shader (stylus-osm.yaml, style forest-trees) ray-marches these as a parallax-occlusion height field draped
on the forest polygon, so crowns have real depth, hard pointed silhouettes and move with perspective; the mip
chain is the distance anti-aliasing (a box-filtered canopy), replacing any analytic fade.

Channels (periodic over the tile, which spans TILE_M metres at u_tree_spacing == 6):
  R = crown height, 0 = ground gap .. 1 = tallest tip
  G = d(R)/du, B = d(R)/dv   (u,v in tile widths), encoded 0.5 + g / (2 * GRAD_RANGE)  -> shader GRAD_RANGE below
  A = per-crown tone id (0..1, ~0.5 mean)
"""
import numpy as np
from PIL import Image

SIZE = 1024
TILE_M = 128.0
GRAD_RANGE = 100.0     # keep in sync with the shader (TREE_GRAD_RANGE)
TEXEL = TILE_M / SIZE


def place(rng, cell_m, jitter, skip):
    n = int(TILE_M / cell_m)
    c = TILE_M / n
    pts = []
    for j in range(n):
        for i in range(n):
            if rng.uniform() < skip:
                continue
            pts.append(((i + 0.5 + rng.uniform(-jitter, jitter)) * c, (j + 0.5 + rng.uniform(-jitter, jitter)) * c))
    return pts


CROWNS = []


def stamp(H, T, I, cx, cy, Rmax, fn, tone, hid):
    CROWNS.append((cx, cy, Rmax, hid))
    w = int(np.ceil(Rmax / TEXEL)) + 2
    px, py = int(round(cx / TEXEL)), int(round(cy / TEXEL))
    xs = np.arange(px - w, px + w + 1); ys = np.arange(py - w, py + w + 1)
    X, Y = np.meshgrid((xs * TEXEL - cx), (ys * TEXEL - cy))
    h = fn(X, Y)
    ix, iy = np.ix_(ys % SIZE, xs % SIZE)
    cur = H[iy, ix]
    win = h > cur
    H[iy, ix] = np.where(win, h, cur)
    T[iy, ix] = np.where(win, tone, T[iy, ix])
    cid = I[iy, ix]                                   # crown id map: centre offset (m), hash
    new = np.stack([-X, -Y, np.full_like(X, hid), np.ones_like(X)], -1)
    I[iy, ix] = np.where(win[..., None], new, cid)


def conifer_tile(seed):
    CROWNS.clear()
    rng = np.random.default_rng(seed); rh = np.random.default_rng(seed + 1000)   # rh: crown ids, separate so the shapes stay stable
    H = np.zeros((SIZE, SIZE), np.float32); T = np.full((SIZE, SIZE), 0.5, np.float32); I = np.zeros((SIZE, SIZE, 4), np.float32)
    for (cx, cy) in place(rng, 3.2, 0.42, 0.05):
        R = rng.uniform(0.95, 1.7); top = rng.uniform(0.55, 1.0)
        R *= 0.8 + 0.4 * top
        a1, a2, a3 = rng.uniform(0, 6.28, 3)
        q = rng.uniform(0.0, 1.0); lean = rng.uniform(-0.12, 0.12, 2) * R

        def fn(X, Y, R=R, top=top, a1=a1, a2=a2, a3=a3, lean=lean):
            ang = np.arctan2(Y, X)
            Reff = R * (1 + 0.20 * np.cos(7 * ang + a1) + 0.10 * np.cos(13 * ang + a2) + 0.05 * np.cos(23 * ang + a3))
            d = np.hypot(X - lean[0] * 0, Y) / Reff
            tier = 1.0 - 0.10 * ((d * 4.5 + a1) % 1.0)
            h = top * np.clip(1.0 - d, 0, 1) ** 1.25 * tier
            return np.where(d < 1.0, h, 0.0).astype(np.float32)
        stamp(H, T, I, cx, cy, R * 1.45, fn, 0.5 + rng.uniform(-0.2, 0.2), rh.uniform())
    fill_rim(I)
    return H, T, I


def broad_tile(seed):
    CROWNS.clear()
    rng = np.random.default_rng(seed); rh = np.random.default_rng(seed + 1000)   # rh: crown ids, separate so the shapes stay stable
    H = np.zeros((SIZE, SIZE), np.float32); T = np.full((SIZE, SIZE), 0.5, np.float32); I = np.zeros((SIZE, SIZE, 4), np.float32)
    for (cx, cy) in place(rng, 5.0, 0.4, 0.05):
        R = rng.uniform(2.0, 3.4); top = rng.uniform(0.45, 0.7)
        lobes = [(rng.uniform(-0.5, 0.5) * R, rng.uniform(-0.5, 0.5) * R, rng.uniform(0.55, 0.85) * R, top * rng.uniform(0.8, 1.0)) for _ in range(4)]
        ph = rng.uniform(0, 6.28, 2)

        def fn(X, Y, lobes=lobes, ph=ph):
            h = np.zeros_like(X)
            for (ox, oy, r, t) in lobes:
                ang = np.arctan2(Y - oy, X - ox)
                d = np.hypot(X - ox, Y - oy) / (r * (1 + 0.12 * np.cos(5 * ang + ph[0]) + 0.06 * np.cos(11 * ang + ph[1])))
                h = np.maximum(h, np.where(d < 1, t * np.sqrt(np.clip(1 - d * d, 0, 1)) ** 0.8, 0))
            return h.astype(np.float32)
        stamp(H, T, I, cx, cy, 3.6 * 1.5, fn, 0.5 + rng.uniform(-0.2, 0.2), rh.uniform())
    fill_rim(I)
    return H, T, I


def fill_rim(I):
    """Texels outside every crown (a == 0) within 1.6 Rmax of a crown inherit that crown's id with a = 0.5: the shader's mip-blurred
    height field is non-zero a little outside the crown footprint, and a removed (thinned) crown must remove that halo too."""
    best = np.full((SIZE, SIZE), 1e9, np.float32)
    for (cx, cy, Rmax, hid) in CROWNS:
        w = int(np.ceil(1.6 * Rmax / TEXEL)) + 2
        px, py = int(round(cx / TEXEL)), int(round(cy / TEXEL))
        xs = np.arange(px - w, px + w + 1); ys = np.arange(py - w, py + w + 1)
        X, Y = np.meshgrid((xs * TEXEL - cx), (ys * TEXEL - cy))
        d = np.hypot(X, Y)
        ix, iy = np.ix_(ys % SIZE, xs % SIZE)
        cur_a = I[iy, ix, 3]; cur_best = best[iy, ix]
        take = (cur_a < 0.75) & (d < 1.6 * Rmax) & (d < cur_best)
        new = np.stack([-X, -Y, np.full_like(X, hid), np.full_like(X, 0.5)], -1)
        I[iy, ix] = np.where(take[..., None], new, I[iy, ix])
        best[iy, ix] = np.where(take, d, cur_best)


ID_RANGE = 6.0         # crown centre offsets are encoded 0.5 + off / (2 * ID_RANGE)   (keep in sync with the shader)


def save_id(path, I):
    img = np.stack([np.clip(0.5 + I[..., 0] / (2 * ID_RANGE), 0, 1), np.clip(0.5 + I[..., 1] / (2 * ID_RANGE), 0, 1),
                    I[..., 2], I[..., 3]], -1)
    Image.fromarray((img * 255 + 0.5).astype(np.uint8), 'RGBA').save(path)


def save(path, H, T):
    Hn = np.clip(H, 0, 1)
    gu = (np.roll(Hn, -1, 1) - np.roll(Hn, 1, 1)) * 0.5 * SIZE     # per tile width
    gv = (np.roll(Hn, -1, 0) - np.roll(Hn, 1, 0)) * 0.5 * SIZE
    enc = lambda g: np.clip(0.5 + g / (2 * GRAD_RANGE), 0, 1)
    img = np.stack([Hn, enc(gu), enc(gv), np.clip(T, 0, 1)], -1)
    Image.fromarray((img * 255 + 0.5).astype(np.uint8), 'RGBA').save(path)


if __name__ == '__main__':
    for name, fn_, seed in (('conifer', conifer_tile, 11), ('broadleaf', broad_tile, 23)):
        H, T, I = fn_(seed)
        save('assets/textures/tree_%s.png' % name, H, T)
        save_id('assets/textures/tree_%s_id.png' % name, I)
    print('ok')
