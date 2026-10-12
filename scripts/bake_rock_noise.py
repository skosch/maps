#!/usr/bin/env python3
"""Bake the rock-texture style's ridged multifractal into a tileable RGBA8 PNG.

Why: the stylus-osm `rock-texture` fragment shader used to evaluate ~11 octaves of
gradient noise per fragment (4 sin() hashes each) - 56% of GPU time in a tilted view
on an Iris Xe. The field is a pure function of position, so it is baked here once and
the shader does one trilinear fetch per triplanar plane; the mip chain replaces the
old hand-rolled per-octave screen-space fade (a box-filtered band-limit of the field,
and the gradient channels filter identically, so bump lighting fades the same way).

Channels (all wrap-periodic over [0,1)^2 == CELLS base cells):
  R = ridged-fBm value, normalized to ~[0,1]
  G = d(value)/dp.x, B = d(value)/dp.y  (p in base-octave cells), encoded as
      0.5 + g / (2 * GRAD_RANGE)  -> shader decodes with GRAD_RANGE below
  A = low-frequency [0,1] gradient-noise mask (lichen patches), 1 cell == 1 mask cell

Same cascade as the shader's old rockRidgedFbm (Musgrave weights, (1-|n|)^2 fold,
GAIN 0.55, WEIGHT_GAIN 3.0), but LAC == 2 exactly and per-octave seeds instead of
inter-octave domain rotation: both are required for exact periodicity. The shader's
three differently-rotated triplanar planes re-break lattice alignment at runtime.
"""
import sys
import numpy as np
from PIL import Image

SIZE = 1024          # texels per side
CELLS = 16           # base-octave cells per texture period (must match shader ROCK_TEX_CELLS)
OCTAVES = 5
GAIN = 0.55
WEIGHT_GAIN = 3.0
GRAD_RANGE = 8.0     # must match shader ROCK_GRAD_RANGE


def gradient_lattice(rng, n):
    """n x n random unit-ish gradient vectors (periodic by construction)."""
    a = rng.uniform(0, 2 * np.pi, (n, n))
    r = np.sqrt(rng.uniform(0.25, 1.0, (n, n)))   # avoid near-zero gradients
    return np.stack([np.cos(a) * r, np.sin(a) * r], -1)


def noised(grads, n, size):
    """Periodic Perlin noise with analytic derivs on a size x size grid; the lattice has
    n cells across the texture. Returns value, dvalue/dx, dvalue/dy (x,y in LATTICE cells)."""
    t = (np.arange(size) + 0.5) / size * n
    x, y = np.meshgrid(t, t)
    xi, yi = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = x - xi, y - yi
    u = fx**3 * (fx * (fx * 6 - 15) + 10)
    v = fy**3 * (fy * (fy * 6 - 15) + 10)
    du = 30 * fx**2 * (fx * (fx - 2) + 1)
    dv = 30 * fy**2 * (fy * (fy - 2) + 1)

    def g(ox, oy):
        return grads[(yi + oy) % n, (xi + ox) % n]

    ga, gb, gc, gd = g(0, 0), g(1, 0), g(0, 1), g(1, 1)
    va = ga[..., 0] * fx + ga[..., 1] * fy
    vb = gb[..., 0] * (fx - 1) + gb[..., 1] * fy
    vc = gc[..., 0] * fx + gc[..., 1] * (fy - 1)
    vd = gd[..., 0] * (fx - 1) + gd[..., 1] * (fy - 1)
    k1, k2, k3 = vb - va, vc - va, va - vb - vc + vd
    val = va + k1 * u + k2 * v + k3 * u * v
    # d/dx and d/dy include the gradient-vector terms (the shader's old rockNoised
    # dropped them - its derivative was only the interpolation-weight part; the exact
    # derivative is strictly better and costs nothing at bake time).
    dx = ga[..., 0] + (gb[..., 0] - ga[..., 0]) * u + (gc[..., 0] - ga[..., 0]) * v \
        + (ga[..., 0] - gb[..., 0] - gc[..., 0] + gd[..., 0]) * u * v \
        + du * (k1 + k3 * v)
    dy = ga[..., 1] + (gb[..., 1] - ga[..., 1]) * u + (gc[..., 1] - ga[..., 1]) * v \
        + (ga[..., 1] - gb[..., 1] - gc[..., 1] + gd[..., 1]) * u * v \
        + dv * (k2 + k3 * u)
    return val, dx, dy


def main(out):
    rng = np.random.default_rng(20261010)
    total = np.zeros((SIZE, SIZE))
    gx = np.zeros((SIZE, SIZE))
    gy = np.zeros((SIZE, SIZE))
    norm = 0.0
    amp = 0.5
    weight = np.ones((SIZE, SIZE))
    for k in range(OCTAVES):
        n = CELLS * 2**k
        val, dx, dy = noised(gradient_lattice(rng, n), n, SIZE)
        # lattice cell -> base cell: d/dp = 2^k * d/dlattice
        dx *= 2**k
        dy *= 2**k
        ax = np.clip(np.abs(val), 0, 1)
        nn = (1 - ax) ** 2
        sx = np.sign(val)
        gxk = -2 * (1 - ax) * sx * dx
        gyk = -2 * (1 - ax) * sx * dy
        nn = nn * weight
        gxk, gyk = gxk * weight, gyk * weight
        weight = np.clip(nn * WEIGHT_GAIN, 0, 1)
        total += amp * nn
        gx += amp * gxk
        gy += amp * gyk
        norm += amp
        amp *= GAIN
    total /= norm
    gx /= norm
    gy /= norm

    # lichen mask: 1 mask cell = 1 base cell, matching the old "world / 60 m" tap only
    # up to a shader-side scale (shader samples this channel with its own uv scale).
    lv, _, _ = noised(gradient_lattice(rng, CELLS), CELLS, SIZE)
    lichen = np.clip(0.5 + lv * 2.0, 0, 1)

    print(f"value mean {total.mean():.3f} std {total.std():.3f}; "
          f"grad |max| {max(abs(gx).max(), abs(gy).max()):.2f} (range +-{GRAD_RANGE}); "
          f"clipped {(np.abs(gx) > GRAD_RANGE).mean() + (np.abs(gy) > GRAD_RANGE).mean():.4%}")
    enc = lambda g: np.clip(0.5 + g / (2 * GRAD_RANGE), 0, 1)
    img = np.stack([np.clip(total, 0, 1), enc(gx), enc(gy), lichen], -1)
    Image.fromarray((img * 255 + 0.5).astype(np.uint8), "RGBA").save(out, optimize=True)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "assets/textures/rock_ridged_noise.png")
