"""
count_filters.py  -  count round filters (tubes) packed in a hexagonal pattern in a box.

HOW IT WORKS
  1. measure the tube spacing (FFT of the photo)
  2. find the bright tube tops (difference-of-gaussians blob detector)
  3. fit a hexagonal lattice (with perspective) to those tops
  4. walk outward over every lattice site and keep the ones that really contain a tube
     (right brightness + colour + REAL tube-top contrast) - a missed detection is therefore recovered,
     and the cardboard / floor around the box is rejected
  5. cut everything outside the outline of the real tubes (convex hull) and outside the corners you clicked
  6. draw a numbered circle on every counted tube (red / green / blue cycle), like countthings.com

The count = number of circles drawn.

USAGE
  python count_filters.py photo.jpg            (opens the review window)
  python count_filters.py photo.jpg --nogui    (just saves count_result.jpg)
  python count_filters.py photo.jpg --manual   (you click the 4 corners of the tube area first - MOST RELIABLE)
  python count_filters.py photo.jpg --manual --nogui   (click the corners, then save the result without the review window)
  python count_filters.py photo.jpg --pitch 17 (tube spacing in photo pixels, if auto-measure fails)
  python count_filters.py photo.jpg --debug    (also saves count_debug.jpg: shows which lattice sites were rejected)
  python count_filters.py photo.jpg --box 600 330 --diameter 7.8   (also prints the theoretical maximum for the box, mm)

REVIEW WINDOW
  mouse wheel / + -  zoom        arrows / W A S D  move       f  fit whole photo
  left click  = ADD a tube that was missed        right click = REMOVE a wrong circle
  s  save count_result.jpg       m  full-screen on/off        q / Esc  quit

REQUIREMENTS   pip install opencv-python numpy
"""
import sys, argparse
from pathlib import Path
from collections import deque
import cv2, numpy as np

DEFAULT_IMAGE = r"C:\Users\DELLL\Downloads\python\test\6.jpeg"
FILTER_DIAMETER_MM = 7.80

# ------------------------------------------------------------------ settings
WORK_PITCH      = 18.0   # while detecting, the photo is rescaled so tubes are ~this many px apart
PEAK_REL        = 0.30   # weakest tube-top peak that is used for the lattice fit (fraction of a strong peak)
MATCH_TOL       = 0.30   # a detected peak belongs to a lattice site if it is within this fraction of the pitch
LUM_MIN         = 0.70   # CHANGED a site holds a tube if it is at least this fraction as bright as the tubes AROUND it (local, not global)
CHROMA_TOL_MIN  = 12.0   # ... and its colour is close to the typical tube colour (Lab a/b distance)
COVER_R         = 0.30   # radius (x pitch) of the disc that is checked around every lattice site
COVER_MIN       = 0.50   # CHANGED the disc must be at least this fraction tube-white
SEED_TOL        = 0.20   # seed peaks must sit this close (x pitch) to their lattice site
TEX_REL         = 0.30   # CHANGED a site needs at least this fraction of the LOCAL raw tube-top contrast.
                         #   Smooth floor / cardboard has almost none. Raise to reject more, lower (0.2) if tubes are lost.
REF_SIGMA       = 3.0    # NEW  size (x pitch) of the area used as "tubes around it" for brightness / contrast. Handles dark
                         #   sides, shadows and glare. Lower (2.0) if the light changes very quickly across the box.
FILL_COVER      = 0.40   # NEW  gap filling: an EMPTY spot inside the block is still filled if it is at least this tube-white
                         #   (glare / weak blobs). A truly empty hole is dark, so it is NOT filled.
EDGE_TEX        = 0.15   # NEW  missing tubes on the outer edge also need this fraction of the local tube-top contrast
BOX_EXTEND      = 6      # NEW  how many extra rows / columns beyond the counted tubes the box fill may grow while they still look
                         #   tube-white (recovers whole over-exposed rows at the box edge). It stops at the dark rim by itself.
SIDE_FRAC       = 0.60   # NEW  a new row / column is added only if at least this fraction of its sites look tube-white
BOX_FILL        = True   # NEW  fill the whole box rectangle (in lattice space) where it is still tube-white. This recovers
                         #   OVER-EXPOSED / glare areas where the tubes have no visible detail left. Set False to disable.
EDGE_ITERS      = 3      # NEW  how many times the outer edge is re-checked for missed tubes (1 column each time, at most)
HULL_MARGIN     = 1.00   # counted tubes may stick out at most this far (x pitch) beyond the outline of the
                         #   real detected tubes. Lower (0.6) if circles still appear outside the box.
ROI_MARGIN      = 0.20   # CHANGED (was 0.5) with --manual, how far (x pitch) outside the clicked corners a tube may sit
START_FULLSCREEN = False

COLORS = [(0, 0, 255), (0, 190, 0), (255, 0, 0)]       # BGR: red, green, blue  (cycled by tube number)
NB = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1)]   # the 6 hexagonal neighbours in (i, j)

# ================================================================== io / screen
def load_image(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"File not found: {p.resolve()}\n(running from: {Path.cwd()})")
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"Found the file but it is not a readable image: {p}")
    return img

def save_image(path, img):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if ok: buf.tofile(str(path))

def screen_size():
    try:
        import ctypes
        u = ctypes.windll.user32
        try: u.SetProcessDPIAware()
        except Exception: pass
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080

def apply_h(H, xy):
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    q = np.c_[xy, np.ones(len(xy))] @ H.T
    return q[:, :2] / q[:, 2:3]

# ================================================================== tube spacing (FFT)
def flat_gray(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return g - cv2.GaussianBlur(g, (0, 0), max(15, min(g.shape) / 12))

def _patch_pitch(patch):
    """tube spacing of one patch from the ring of its power spectrum; returns (pitch, how_peaky)"""
    p = patch - patch.mean()
    p = p * np.outer(np.hanning(p.shape[0]), np.hanning(p.shape[1]))
    N = 1024
    P = np.fft.fftshift(np.abs(np.fft.fft2(p, s=(N, N))) ** 2)
    yy, xx = np.indices(P.shape)
    r = np.hypot(yy - N // 2, xx - N // 2).astype(int).ravel()
    ps = np.bincount(r, P.ravel()) / np.maximum(np.bincount(r), 1)
    k = np.arange(len(ps))
    klo, khi = int(N * 2 / (np.sqrt(3) * 90)), int(N * 2 / (np.sqrt(3) * 5))   # spacing 5 .. 90 px
    lp = np.log(ps + 1e-9)
    base = cv2.GaussianBlur(lp.reshape(-1, 1).astype(np.float32), (0, 0), 10).ravel()
    sc = lp - base
    kk = k[(k > klo) & (k < khi)]
    f = kk[np.argmax(sc[kk])]
    w = ps[f - 2:f + 3]; ks = k[f - 2:f + 3]
    f_sub = float((w * ks).sum() / w.sum())
    return 2.0 * N / (np.sqrt(3) * f_sub), float(sc[f])

def estimate_pitch(g):
    h, w = g.shape
    s = int(min(384, h, w))
    res = []
    for y in np.linspace(0, h - s, 4).astype(int):
        for x in np.linspace(0, w - s, 4).astype(int):
            try: res.append(_patch_pitch(g[y:y + s, x:x + s]))
            except Exception: pass
    if not res: raise RuntimeError("could not measure the tube spacing")
    res.sort(key=lambda t: -t[1])
    return float(np.median([r[0] for r in res[:5]]))

# ================================================================== tube tops
def raw_dog(g, p):
    """NEW: plain difference-of-gaussians (NOT normalised). Real tube tops give a strong value,
    smooth floor / cardboard gives almost nothing - this is what the texture test uses."""
    return cv2.GaussianBlur(g, (0, 0), 0.20 * p) - cv2.GaussianBlur(g, (0, 0), 0.42 * p)

def blob_response(g, p):
    """difference-of-gaussians blob map, divided by a ROBUST local contrast level.
    The level is clipped so the strong box rim / shadows cannot flatten the faint tubes
    in washed-out (over-exposed) areas."""
    r = raw_dog(g, p)
    a = np.abs(r)
    lvl = cv2.GaussianBlur(a, (0, 0), 1.5 * p)
    for _ in range(2):
        lvl = cv2.GaussianBlur(np.minimum(a, 1.5 * lvl + 0.3), (0, 0), 1.5 * p)
    return r / (lvl + 0.3)

def find_peaks(R, p):
    k = max(3, int(round(0.62 * p)) | 1)
    mx = cv2.dilate(R, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    loc = (R >= mx) & (R > 0)
    thr = PEAK_REL * np.percentile(R[loc], 90)
    ys, xs = np.nonzero(loc & (R > thr))
    h, w = R.shape
    keep = (xs > 2) & (xs < w - 3) & (ys > 2) & (ys < h - 3)
    xs, ys = xs[keep], ys[keep]
    c = R[ys, xs]
    def sub(a, b, cc):                                   # parabola vertex offset
        d = a - 2 * cc + b
        return np.where(d < -1e-6, 0.5 * (a - b) / d, 0.0).clip(-1, 1)
    dx = sub(R[ys, xs - 1], R[ys, xs + 1], c)
    dy = sub(R[ys - 1, xs], R[ys + 1, xs], c)
    return np.c_[xs + dx, ys + dy].astype(np.float64)

# ================================================================== lattice
def lattice_basis(pts, p):
    """refined tube spacing and the angle of the tube rows (hexagonal lattice -> 6-fold angle average)"""
    cell = 1.5 * p
    keys = np.floor(pts / cell).astype(int)
    grid = {}
    for n, (kx, ky) in enumerate(keys): grid.setdefault((int(kx), int(ky)), []).append(n)
    vec = []
    for n in range(len(pts)):
        kx, ky = int(keys[n][0]), int(keys[n][1])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in grid.get((kx + dx, ky + dy), ()):
                    if m > n:
                        d = pts[m] - pts[n]; L = np.hypot(d[0], d[1])
                        if 0.7 * p < L < 1.35 * p: vec.append(d)
    if len(vec) < 20: raise RuntimeError("not enough regular tube tops found (try --pitch)")
    vec = np.array(vec)
    L = np.hypot(vec[:, 0], vec[:, 1]); ang = np.arctan2(vec[:, 1], vec[:, 0])
    theta = np.angle(np.exp(6j * ang).sum()) / 6.0            # rows are the lattice direction nearest to horizontal
    return float(np.median(L)), float(theta)

def _best_per_site(ij, err, ok):
    """if several peaks claim the same lattice site keep the closest"""
    idx = np.nonzero(ok)[0]
    order = idx[np.lexsort((err[idx], ij[idx, 1], ij[idx, 0]))]
    seen, out = set(), []
    for n in order:
        key = (int(ij[n, 0]), int(ij[n, 1]))
        if key not in seen: seen.add(key); out.append(n)
    return np.array(out, int)

def fit_homography(pts, p, theta):
    """grow the lattice outward from the middle, refitting a perspective map (i,j)->(x,y) at every step"""
    a1 = p * np.array([np.cos(theta), np.sin(theta)])
    a2 = p * np.array([np.cos(theta + np.pi / 3), np.sin(theta + np.pi / 3)])
    c = np.median(pts, 0); seed = pts[np.argmin(((pts - c) ** 2).sum(1))]
    H = np.array([[a1[0], a2[0], seed[0]], [a1[1], a2[1], seed[1]], [0, 0, 1]], float)
    R, extra = 6.0, 0
    while True:
        uv = apply_h(np.linalg.inv(H), pts); ij = np.round(uv)
        err = np.hypot(*(pts - apply_h(H, ij)).T)
        dist = np.sqrt(ij[:, 0] ** 2 + ij[:, 1] ** 2 + ij[:, 0] * ij[:, 1])
        sel = _best_per_site(ij, err, (err < MATCH_TOL * p) & (dist <= R))
        if len(sel) >= 10:
            Hn, mk = cv2.findHomography(ij[sel], pts[sel], cv2.RANSAC, 0.25 * p)
            if Hn is not None:
                inl = sel[mk.ravel().astype(bool)]
                if len(inl) >= 10:
                    Hl, _ = cv2.findHomography(ij[inl], pts[inl], 0)
                    if Hl is not None: H = Hl
        if R >= dist.max() * 1.05: extra += 1
        if extra >= 2 or R > 1e4: break
        R *= 1.5
    return H

class Lattice:
    """perspective map + a smooth correction field (lens distortion, uneven boxes)"""
    def __init__(self, H, p):
        self.H, self.Hi, self.p = H, np.linalg.inv(H), p
        self.cx = self.cy = None

    def base(self, ij): return apply_h(self.H, ij)

    def set_residuals(self, ij, xy):
        res = xy - self.base(ij)
        i0, j0 = ij.min(0).astype(int) - 8; i1, j1 = ij.max(0).astype(int) + 8
        w, h = i1 - i0 + 1, j1 - j0 + 1
        RX = np.zeros((h, w), np.float32); RY = RX.copy(); M = RX.copy()
        ii = (ij[:, 0] - i0).astype(int); jj = (ij[:, 1] - j0).astype(int)
        RX[jj, ii], RY[jj, ii], M[jj, ii] = res[:, 0], res[:, 1], 1
        den = np.maximum(cv2.GaussianBlur(M, (0, 0), 2.5), 0.02)
        self.cx = cv2.GaussianBlur(RX, (0, 0), 2.5) / den
        self.cy = cv2.GaussianBlur(RY, (0, 0), 2.5) / den
        self.off = (i0, j0)

    def pos(self, ij):
        ij = np.asarray(ij, float).reshape(-1, 2)
        out = self.base(ij)
        if self.cx is not None:
            h, w = self.cx.shape
            ii = np.clip(ij[:, 0].astype(int) - self.off[0], 0, w - 1)
            jj = np.clip(ij[:, 1].astype(int) - self.off[1], 0, h - 1)
            out = out + np.c_[self.cx[jj, ii], self.cy[jj, ii]]
        return out

    def assign(self, pts, tol=None):
        ij = np.round(apply_h(self.Hi, pts))
        err = np.hypot(*(pts - self.pos(ij)).T)
        sel = _best_per_site(ij, err, err < (tol or MATCH_TOL) * self.p)
        return ij[sel].astype(int), sel

def build_lattice(pts, p, theta):
    lat = Lattice(fit_homography(pts, p, theta), p)
    ij, sel = lat.assign(pts)
    for it in range(4):
        Hn, mk = cv2.findHomography(ij.astype(np.float64), pts[sel], cv2.RANSAC, 0.3 * p)
        Hl, _ = cv2.findHomography(ij[mk.ravel().astype(bool)].astype(np.float64), pts[sel][mk.ravel().astype(bool)], 0)
        lat = Lattice(Hl, p)
        lat.set_residuals(ij.astype(float), pts[sel])
        ij, sel = lat.assign(pts, MATCH_TOL if it < 2 else 0.38)
    return lat, ij, sel

# ================================================================== which lattice sites really hold a tube
def analyse(img, pitch_px=None, roi=None, log=print):
    """returns dict with: xy (n,2) tube centres in ORIGINAL photo pixels, ij (n,2), pitch (orig px), lattice info"""
    H0, W0 = img.shape[:2]
    s0 = min(1.0, 1600.0 / max(H0, W0))
    small = cv2.resize(img, None, fx=s0, fy=s0, interpolation=cv2.INTER_AREA) if s0 < 1 else img
    p_orig = float(pitch_px) if pitch_px else estimate_pitch(flat_gray(small)) / s0
    if not np.isfinite(p_orig) or p_orig <= 0:                       # NEW: readable error for photos with no tube pattern
        raise RuntimeError("no regular tube pattern found in the photo - is the box in the picture?")
    scale = float(np.clip(WORK_PITCH / p_orig, 0.2, 3.0)); scale = min(scale, 3500.0 / max(H0, W0))
    work = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    p = p_orig * scale
    if not pitch_px:
        try: p = estimate_pitch(flat_gray(work))
        except Exception: pass

    g = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY).astype(np.float32)
    R = blob_response(g, p)
    Rraw = raw_dog(g, p)                                   # NEW: un-normalised contrast, used by the texture test
    pts = find_peaks(R, p)
    if len(pts) < 30: raise RuntimeError("found almost no tube tops - check the photo / try --pitch")
    p, theta = lattice_basis(pts, p)
    lat, ij_m, sel = build_lattice(pts, p, theta)
    log(f"  tube spacing {p / scale:.1f}px (photo)   tube tops found {len(pts)}   fitted to lattice {len(sel)}")

    # --- typical tube: brightness, colour, blob response (from the tube tops that fit the lattice)
    lab = cv2.cvtColor(cv2.GaussianBlur(work, (0, 0), 0.15 * p), cv2.COLOR_BGR2LAB).astype(np.float32)
    hh, ww = g.shape
    def samp(M, xy):
        x = np.clip(np.round(xy[:, 0]).astype(int), 0, ww - 1); y = np.clip(np.round(xy[:, 1]).astype(int), 0, hh - 1)
        return M[y, x]
    mxy = pts[sel]
    chroma = np.hypot(lab[..., 1] - 128.0, lab[..., 2] - 128.0)          # how coloured a pixel is (tubes: ~neutral)
    c_m = samp(chroma, mxy)
    chroma_max = max(CHROMA_TOL_MIN, np.percentile(c_m, 90) + 8.0)       # whiter than typical is fine, browner is not

    def local_ref(M):
        """NEW: smooth map of the 'typical tube value' AROUND every pixel (computed from the detected tube tops).
        Light is never even - dark side, shadow, glare - so a site is compared with its neighbours, not with the whole box.
        Far from any tube the global median is used, so the floor / cardboard still gets the strict test."""
        vals = samp(M, mxy).astype(np.float32); glob = float(np.median(vals))
        f = 4
        sh_, sw_ = max(2, hh // f), max(2, ww // f)
        num = np.zeros((sh_, sw_), np.float32); den = np.zeros((sh_, sw_), np.float32)
        gx = np.clip((mxy[:, 0] / f).astype(int), 0, sw_ - 1); gy = np.clip((mxy[:, 1] / f).astype(int), 0, sh_ - 1)
        np.add.at(num, (gy, gx), vals); np.add.at(den, (gy, gx), 1.0)
        sg = REF_SIGMA * p / f
        num = cv2.GaussianBlur(num, (0, 0), sg); den = cv2.GaussianBlur(den, (0, 0), sg)
        dtyp = max(float(np.median(den[gy, gx])), 1e-9)
        wgt = np.clip(den / (0.5 * dtyp), 0, 1)
        loc = num / np.maximum(den, 1e-12)
        out = wgt * loc + (1 - wgt) * glob
        return cv2.resize(out, (ww, hh), interpolation=cv2.INTER_LINEAR)
    L_ref = local_ref(lab[..., 0])                                       # CHANGED: local brightness reference
    tex_ref = np.maximum(local_ref(Rraw), 1e-3)                          # CHANGED: local tube-top contrast reference

    white = ((lab[..., 0] >= LUM_MIN * L_ref) & (chroma <= chroma_max)).astype(np.float32)
    rk = max(2, int(round(COVER_R * p)))
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rk + 1, 2 * rk + 1)).astype(np.float32)
    cover = cv2.filter2D(white, -1, disc / disc.sum(), borderType=cv2.BORDER_REPLICATE)

    rk2 = max(1, int(round(0.25 * p)))
    rn_max = cv2.dilate(Rraw, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rk2 + 1, 2 * rk2 + 1)))   # raw contrast

    def present(xy):
        """a site holds a tube when a small disc around it is (almost) all tube-white AND it shows real tube-top contrast
        (both compared with the tubes around it)"""
        return (samp(cover, xy) >= COVER_MIN) & (samp(rn_max, xy) >= TEX_REL * samp(tex_ref, xy))

    roi_w = None if roi is None else (np.asarray(roi, np.float32) * scale).reshape(-1, 1, 2)
    def inside(x, y):
        if not (0.3 * p <= x <= ww - 1 - 0.3 * p and 0.3 * p <= y <= hh - 1 - 0.3 * p): return False
        return roi_w is None or cv2.pointPolygonTest(roi_w, (float(x), float(y)), True) >= -ROI_MARGIN * p   # CHANGED: tighter

    peak_of = {(int(i), int(j)): n for (i, j), n in zip(ij_m, sel)}
    cache = {}
    def site(ij):
        """(position, ok) of lattice site ij, cached"""
        if ij in cache: return cache[ij]
        xy = lat.pos([ij])[0]
        if ij in peak_of and np.hypot(*(pts[peak_of[ij]] - xy)) <= 0.15 * p: xy = pts[peak_of[ij]]
        ok = inside(xy[0], xy[1]) and bool(present(xy.reshape(1, 2))[0])
        cache[ij] = (xy, ok)
        return cache[ij]

    # --- flood fill over the lattice, starting from the sites that were fitted
    # seeds = sites with a TIGHT peak match; only the largest connected block of them is the box
    seeds = set()
    for ij, n in peak_of.items():
        if np.hypot(*(pts[n] - lat.pos([ij])[0])) <= SEED_TOL * p and site(ij)[1]: seeds.add(ij)
    best, seen = [], set()
    for st0 in seeds:
        if st0 in seen: continue
        grp, stack = [], [st0]; seen.add(st0)
        while stack:
            q = stack.pop(); grp.append(q)
            for di, dj in NB:
                nb = (q[0] + di, q[1] + dj)
                if nb in seeds and nb not in seen: seen.add(nb); stack.append(nb)
        if len(grp) > len(best): best = grp
    if not best: raise RuntimeError("could not find the tube block")
    accepted, queue = {}, deque()
    for ij in best:
        accepted[ij] = site(ij)[0]; queue.append(ij)
    tested = set(accepted) | {k for k, v in cache.items() if not v[1]}   # accepted or rejected; passed-but-unvisited sites must still be flooded
    while queue:
        i, j = queue.popleft()
        for di, dj in NB:
            nb = (i + di, j + dj)
            if nb in tested: continue
            tested.add(nb)
            xy, ok = site(nb)
            if ok: accepted[nb] = xy; queue.append(nb)
    pruned = []

    # --- NEW: hard boundary. The box is convex, so nothing may lie beyond the outline of the real,
    # tightly-detected tubes (plus a small margin for the outer row). This stops leaks onto cardboard / floor.
    hull = None
    if len(best) >= 3:
        hull = cv2.convexHull(np.array([cache[k][0] for k in best], np.float32))
    def in_hull(xy):
        return hull is None or cv2.pointPolygonTest(hull, (float(xy[0]), float(xy[1])), True) >= -HULL_MARGIN * p
    for k in list(accepted):
        if not in_hull(accepted[k]):
            pruned.append(accepted[k]); del accepted[k]

    # holes: a site that is enclosed by tubes (>=5 of 6 neighbours) and is still tube-white is a tube with a weak blob
    for _ in range(6):
        added = False
        for ij in list(tested):
            if ij in accepted: continue
            if sum((ij[0] + di, ij[1] + dj) in accepted for di, dj in NB) >= 5:
                xy = cache[ij][0] if ij in cache else lat.pos([ij])[0]
                if inside(xy[0], xy[1]) and in_hull(xy) and samp(cover, xy.reshape(1, 2))[0] >= FILL_COVER:
                    accepted[ij] = xy; added = True
        if not added: break
    # keep only the tubes that form ONE connected block with the main box (drops islands on the floor / bags)
    comp, sizes = {}, []
    for start in accepted:
        if start in comp: continue
        cid, stack = len(sizes), [start]; comp[start] = cid; n_c = 0
        while stack:
            q = stack.pop(); n_c += 1
            for di, dj in NB:
                nb = (q[0] + di, q[1] + dj)
                if nb in accepted and nb not in comp: comp[nb] = cid; stack.append(nb)
        sizes.append(n_c)
    big = max(sizes)
    for k in list(accepted):
        if sizes[comp[k]] < 0.25 * big:
            pruned.append(accepted[k]); del accepted[k]

    # a real tube always sits among other tubes: drop dangling sites (leaks onto cardboard / floor)
    changed = True
    while changed:
        changed = False
        for ij in list(accepted):
            n = sum((ij[0] + di, ij[1] + dj) in accepted for di, dj in NB)
            if n < 2:                                   # CHANGED: corner tubes of the box only have 2-3 neighbours
                pruned.append(accepted[ij]); del accepted[ij]; changed = True
    if not accepted: raise RuntimeError("no tubes accepted")
    # --- NEW: recover missed tubes after the cleaning. Candidates are lattice sites touching the counted block:
    #   (1) INSIDE the block's outline (glare, shadow, weak blob): accepted if still tube-white. A truly empty hole is dark -> stays empty.
    #   (2) on the OUTER EDGE (e.g. the darker left side): needs >=3 counted neighbours + a little real tube-top contrast.
    # Each round adds at most one extra ring, so this can never run away over the cardboard / floor.
    def refill():
        ks = np.array(list(accepted), int)
        hull2 = cv2.convexHull(np.array(list(accepted.values()), np.float32))
        new = {}
        for i in range(ks[:, 0].min() - 1, ks[:, 0].max() + 2):
            for j in range(ks[:, 1].min() - 1, ks[:, 1].max() + 2):
                if (i, j) in accepted: continue
                nbn = sum((i + di, j + dj) in accepted for di, dj in NB)
                if nbn < 2: continue
                xy = cache[(i, j)][0] if (i, j) in cache else lat.pos([(i, j)])[0]
                if not (inside(xy[0], xy[1]) and in_hull(xy)): continue
                q = xy.reshape(1, 2)
                if samp(cover, q)[0] < FILL_COVER: continue
                if cv2.pointPolygonTest(hull2, (float(xy[0]), float(xy[1])), True) >= -0.1 * p:
                    new[(i, j)] = xy                                            # inside the block
                elif nbn >= 3 and samp(rn_max, q)[0] >= EDGE_TEX * samp(tex_ref, q)[0]:
                    new[(i, j)] = xy                                            # on the outer edge
        accepted.update(new)
        return len(new)
    for _ in range(1 + EDGE_ITERS):
        if not refill(): break

    # --- NEW: fill the BOX itself. A packed box is a rectangle, and in lattice space (rows/columns of tubes) the counted tubes
    # already touch all four of its sides. Where the photo is over-exposed the tubes have NO detail left (flat white), so no
    # tube top can be detected there - but the lattice still tells us exactly where every tube must be. So every lattice site
    # inside the rectangle that is connected to the counted block and still looks tube-white is counted.
    # Dark sites (an empty gap, cardboard, the box rim) fail the white test and stay empty.
    boxfilled = []
    def box_fill():
        ks = np.array(list(accepted), float)
        X = ks[:, 0] + 0.5 * ks[:, 1]; Y = 0.8660254 * ks[:, 1]          # tube centres, straightened (units of the pitch)
        fr = None
        for phi in (0.0, np.pi / 3, 2 * np.pi / 3):                       # box walls run along one of the 3 tube-row directions
            c_, s_ = np.cos(phi), np.sin(phi)
            u = X * c_ + Y * s_; v = -X * s_ + Y * c_
            area = (u.max() - u.min()) * (v.max() - v.min())
            if fr is None or area < fr[0]: fr = (area, c_, s_, u.min(), u.max(), v.min(), v.max())
        _, c_, s_, u0, u1, v0, v1 = fr
        tol = 0.15
        # every lattice site around the counted block (a margin of BOX_EXTEND rows on all sides)
        Mg = BOX_EXTEND + 2.0
        cs = [((u * c_ - v * s_), (u * s_ + v * c_)) for u in (u0 - Mg, u1 + Mg) for v in (v0 - Mg, v1 + Mg)]
        js = [y_ / 0.8660254 for _, y_ in cs]; is_ = [x_ - 0.5 * (y_ / 0.8660254) for x_, y_ in cs]
        gi, gj = np.meshgrid(np.arange(int(np.floor(min(is_))) - 1, int(np.ceil(max(is_))) + 2),
                             np.arange(int(np.floor(min(js))) - 1, int(np.ceil(max(js))) + 2))
        kk = np.c_[gi.ravel(), gj.ravel()]
        Xc = kk[:, 0] + 0.5 * kk[:, 1]; Yc = 0.8660254 * kk[:, 1]
        uc = Xc * c_ + Yc * s_; vc = -Xc * s_ + Yc * c_
        xyc = lat.pos(kk.astype(float))
        acc = np.array([(int(i), int(j)) in accepted for i, j in kk])
        w_ok = (samp(cover, xyc) >= FILL_COVER) & np.array([inside(x_, y_) for x_, y_ in xyc])
        w_ok |= acc
        # grow the rectangle outward, one row (or half a column) at a time, while the next strip is still tube-white.
        # The dark box rim / cardboard / floor is not white -> the growth stops there by itself.
        for side, step in (("v0", 0.8660254), ("v1", 0.8660254), ("u0", 0.5), ("u1", 0.5)):
            for _ in range(int(BOX_EXTEND * (1.0 if side[0] == "v" else 2.0))):
                inu = (uc >= u0 - tol) & (uc <= u1 + tol); inv = (vc >= v0 - tol) & (vc <= v1 + tol)
                if   side == "v0": strip = inu & (vc >= v0 - step - tol) & (vc < v0 - tol)
                elif side == "v1": strip = inu & (vc <= v1 + step + tol) & (vc > v1 + tol)
                elif side == "u0": strip = inv & (uc >= u0 - step - tol) & (uc < u0 - tol)
                else:              strip = inv & (uc <= u1 + step + tol) & (uc > u1 + tol)
                if strip.sum() < 3 or w_ok[strip].mean() < SIDE_FRAC: break
                if   side == "v0": v0 -= step
                elif side == "v1": v1 += step
                elif side == "u0": u0 -= step
                else:              u1 += step
        inr = (uc >= u0 - tol) & (uc <= u1 + tol) & (vc >= v0 - tol) & (vc <= v1 + tol)
        good = {(int(k[0]), int(k[1])): xyc[n] for n, k in enumerate(kk) if inr[n] and w_ok[n] and not acc[n]}
        queue, added = deque(accepted), 0
        while queue:
            i, j = queue.popleft()
            for di, dj in NB:
                nb = (i + di, j + dj)
                if nb in good and nb not in accepted:
                    accepted[nb] = good[nb]; boxfilled.append(good[nb]); queue.append(nb); added += 1
        return added
    if BOX_FILL: box_fill()
    rejected = [cache[k][0] for k in cache if k not in accepted and not cache[k][1]
                and any((k[0] + di, k[1] + dj) in accepted for di, dj in NB)]
    keys = list(accepted)
    ij_a = np.array(keys, int); xy_a = np.array([accepted[k] for k in keys], float) / scale
    rms = float(np.sqrt(np.mean(np.sum((pts[sel] - lat.pos(ij_m.astype(float))) ** 2, 1)))) / scale
    return dict(xy=xy_a, ij=ij_a, pitch=p / scale, scale=scale, lat=lat, rms=rms,
                n_fitted=len(sel), n_peaks=len(pts), matched=np.array([k in peak_of for k in keys]), boxfilled=np.array(boxfilled, float).reshape(-1, 2) / scale,
                rejected=np.array(rejected, float).reshape(-1, 2) / scale, pruned=np.array(pruned, float).reshape(-1, 2) / scale)

# ================================================================== numbering + drawing
def numbering(ij, xy):
    """1..N, row by row (top to bottom), left to right inside each row"""
    order = sorted(range(len(ij)), key=lambda k: (ij[k][1], ij[k][0], xy[k][0]))
    num = np.zeros(len(ij), int)
    for n, k in enumerate(order, 1): num[k] = n
    return num

def draw_sites(canvas, xy, num, radius, ox, oy, z, show_numbers=True):
    """xy: photo coords; canvas shows photo region starting at (ox,oy) magnified by z"""
    r = max(2, int(round(radius * z)))
    thick = 1 if r < 12 else 2
    fs = 0.0
    if show_numbers and len(num):
        (tw, _), _ = cv2.getTextSize(str(int(num.max())), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 1)
        fs = min(1.0, 1.7 * r / tw)
    H, W = canvas.shape[:2]
    for (x, y), n in zip(xy, num):
        cx, cy = int(round((x - ox) * z)), int(round((y - oy) * z))
        if -r < cx < W + r and -r < cy < H + r:
            cv2.circle(canvas, (cx, cy), r, COLORS[n % 3], thick, cv2.LINE_AA)
            if fs >= 0.28:
                t = str(int(n)); (tw, th), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
                cv2.putText(canvas, t, (cx - tw // 2, cy + th // 2), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 1, cv2.LINE_AA)

def banner(canvas, total):
    cv2.rectangle(canvas, (0, 0), (330, 56), (255, 255, 255), -1)
    cv2.putText(canvas, f"Result: {total}", (10, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 0), 3, cv2.LINE_AA)

def render_result(img, xy, num, pitch):
    """saved picture: the whole photo, enlarged so the numbers can be read, every tube circled + numbered"""
    H, W = img.shape[:2]
    sc = min(max(1.0, 46.0 / pitch), 6000.0 / max(H, W))
    big = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC if sc > 1 else cv2.INTER_AREA)
    draw_sites(big, xy, num, 0.5 * pitch, 0, 0, sc, True)
    banner(big, len(xy))
    return big

def render_debug(img, res):
    """circles = counted tubes.  YELLOW dot = lattice site next to the tubes that FAILED the 'is a tube here' test.
    MAGENTA dot = site that passed but was removed (outside the tube outline, or too few neighbours).
    BLUE dot = tube with NO visible detail (over-exposed / glare) that was counted because the lattice says a tube is there.
    If yellow dots sit on real tubes, lower TEX_REL / LUM_MIN / COVER_MIN (or raise CHROMA_TOL_MIN) at the top of the file.
    If magenta dots sit on real outer tubes, raise HULL_MARGIN."""
    H, W = img.shape[:2]; pitch = res["pitch"]
    sc = min(max(1.0, 30.0 / pitch), 6000.0 / max(H, W))
    big = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC if sc > 1 else cv2.INTER_AREA)
    for x, y in res["xy"]: cv2.circle(big, (int(x * sc), int(y * sc)), max(2, int(0.5 * pitch * sc)), (0, 200, 0), 1, cv2.LINE_AA)
    for pts_, col in ((res["rejected"], (0, 255, 255)), (res["pruned"], (255, 0, 255)), (res["boxfilled"], (255, 160, 0))):
        for x, y in pts_:
            cv2.circle(big, (int(x * sc), int(y * sc)), max(3, int(0.22 * pitch * sc)), col, -1, cv2.LINE_AA)
            cv2.circle(big, (int(x * sc), int(y * sc)), max(3, int(0.22 * pitch * sc)), (0, 0, 0), 1, cv2.LINE_AA)
    banner(big, len(res["xy"]))
    return big

# ================================================================== optional manual corners
def order_corners(p):
    p = np.array(p, np.float32).reshape(4, 2)
    s, d = p.sum(1), np.diff(p, axis=1).ravel()
    return np.array([p[s.argmin()], p[d.argmin()], p[s.argmax()], p[d.argmax()]], np.float32)

def pick_corners(img):
    sw, sh = screen_size()
    sc = min(1.0, sw * 0.92 / img.shape[1], sh * 0.78 / img.shape[0])
    small = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
    pts = []
    def cb(e, x, y, *_):
        if e == cv2.EVENT_LBUTTONDOWN and len(pts) < 4: pts.append((x, y))
    cv2.namedWindow("corners", cv2.WINDOW_AUTOSIZE); cv2.setMouseCallback("corners", cb)
    while len(pts) < 4:
        v = small.copy()
        for q in pts: cv2.circle(v, q, 6, (0, 0, 255), -1)
        cv2.putText(v, f"Click the 4 outer corners of the tube area ({len(pts)}/4)", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
        cv2.imshow("corners", v)
        if cv2.waitKey(30) == 27: sys.exit("cancelled")
    cv2.destroyWindow("corners")
    return order_corners(np.array(pts, np.float32) / sc)

# ================================================================== review window
def compose_view(img, xy, num, pitch, z, ox, oy, vw, vh):
    H, W = img.shape[:2]
    x1, y1 = int(ox), int(oy)
    x2 = min(W, int(np.ceil(ox + vw / z))); y2 = min(H, int(np.ceil(oy + vh / z)))
    crop = cv2.resize(img[y1:y2, x1:x2], None, fx=z, fy=z, interpolation=cv2.INTER_AREA if z < 1 else cv2.INTER_CUBIC)
    ch, cw = min(vh, crop.shape[0]), min(vw, crop.shape[1])
    px = (vw - cw) // 2 if (x1 == 0 and x2 >= W) else 0
    py = (vh - ch) // 2 if (y1 == 0 and y2 >= H) else 0
    canvas = np.full((vh, vw, 3), 40, np.uint8)
    canvas[py:py + ch, px:px + cw] = crop[:ch, :cw]
    if len(xy):
        m = (xy[:, 0] > x1 - pitch) & (xy[:, 0] < x2 + pitch) & (xy[:, 1] > y1 - pitch) & (xy[:, 1] < y2 + pitch)
        draw_sites(canvas, xy[m], num[m], 0.5 * pitch, x1 - px / z, y1 - py / z, z, pitch * z >= 24)
    banner(canvas, len(xy))
    return canvas, px, py

def review(img, res, out_path):
    xy = [tuple(p) for p in res["xy"]]; ij = [tuple(p) for p in res["ij"]]
    pitch, scale, lat = res["pitch"], res["scale"], res["lat"]
    H, W = img.shape[:2]
    sw, sh = screen_size()
    s0 = min((sw * 0.92) / W, (sh * 0.80) / H, max(1.0, 700.0 / max(W, H)))
    wvw, wvh = max(200, int(W * s0)), max(150, int(H * s0))
    win = "review"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    vw, vh = (sw, sh) if START_FULLSCREEN else (wvw, wvh)
    st = {"z": min(vw / W, vh / H), "ox": 0.0, "oy": 0.0, "px": 0, "py": 0, "fitted": True, "dirty": True}
    size = {"w": vw, "h": vh}; fs = {"on": False}

    def set_fullscreen(on):
        fs["on"] = on
        if on:
            cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN); size["w"], size["h"] = sw, sh
        else:
            cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win, wvw, wvh)
            try: cv2.moveWindow(win, max(0, (sw - wvw) // 2), max(0, (sh - wvh) // 4))
            except Exception: pass
            size["w"], size["h"] = wvw, wvh
        st.update(z=min(size["w"] / W, size["h"] / H), ox=0.0, oy=0.0, fitted=True, dirty=True)
    set_fullscreen(START_FULLSCREEN)

    def fit(): return min(size["w"] / W, size["h"] / H)
    def clamp():
        st["z"] = float(np.clip(st["z"], fit(), max(10.0, fit() * 8)))
        st["ox"] = float(np.clip(st["ox"], 0, max(0, W - size["w"] / st["z"])))
        st["oy"] = float(np.clip(st["oy"], 0, max(0, H - size["h"] / st["z"])))
        st["fitted"] = st["z"] <= fit() * 1.001; st["dirty"] = True
    def zoom_at(mx, my, f):
        wx, wy = st["ox"] + (mx - st["px"]) / st["z"], st["oy"] + (my - st["py"]) / st["z"]
        st["z"] *= f; clamp()
        st["ox"], st["oy"] = wx - (mx - st["px"]) / st["z"], wy - (my - st["py"]) / st["z"]; clamp()

    def cb(e, x, y, flags, _):
        wx = st["ox"] + (x - st["px"]) / st["z"]; wy = st["oy"] + (y - st["py"]) / st["z"]
        if e == cv2.EVENT_LBUTTONDOWN and 0 <= wx < W and 0 <= wy < H:        # add a missed tube
            k = np.round(apply_h(lat.Hi, [(wx * scale, wy * scale)])[0])
            xy.append((wx, wy)); ij.append((int(k[0]), int(k[1]))); st["dirty"] = True
        elif e == cv2.EVENT_RBUTTONDOWN and xy:                               # remove a wrong circle
            d = [(px - wx) ** 2 + (py - wy) ** 2 for px, py in xy]; i = int(np.argmin(d))
            if d[i] < (0.6 * pitch) ** 2: xy.pop(i); ij.pop(i); st["dirty"] = True
        elif e == cv2.EVENT_MOUSEWHEEL: zoom_at(x, y, 1.25 if flags > 0 else 0.8)
    cv2.setMouseCallback(win, cb)

    def current():
        a = np.array(xy, float).reshape(-1, 2)
        return a, numbering(ij, xy)

    while True:
        try:
            _, _, rw, rh = cv2.getWindowImageRect(win)
            if rw > 50 and rh > 50 and (rw, rh) != (size["w"], size["h"]):
                size["w"], size["h"] = int(rw), int(rh)
                if st["fitted"]: st["z"] = fit()
                clamp()
        except Exception: pass
        if st["dirty"]:
            a, num = current()
            view, st["px"], st["py"] = compose_view(img, a, num, pitch, st["z"], st["ox"], st["oy"], size["w"], size["h"])
            cv2.imshow(win, view); st["dirty"] = False
        k = cv2.waitKeyEx(30)
        try:
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1: break
        except Exception: pass
        if k == -1: continue
        c = k & 0xFF; step = 80 / st["z"]
        if c in (ord('q'), 27): break
        elif c == ord('s'):
            a, num = current(); save_image(out_path, render_result(img, a, num, pitch)); print("saved", out_path)
        elif c in (ord('+'), ord('=')): zoom_at(size["w"] / 2, size["h"] / 2, 1.25)
        elif c in (ord('-'), ord('_')): zoom_at(size["w"] / 2, size["h"] / 2, 0.8)
        elif c == ord('f'): st.update(z=fit(), ox=0.0, oy=0.0); clamp()
        elif c == ord('m'): set_fullscreen(not fs["on"])
        elif c == ord('a') or k == 2424832: st["ox"] -= step; clamp()
        elif c == ord('d') or k == 2555904: st["ox"] += step; clamp()
        elif c == ord('w') or k == 2490368: st["oy"] -= step; clamp()
        elif c == ord('x') or k == 2621440: st["oy"] += step; clamp()
    cv2.destroyAllWindows()
    return len(xy)

# ================================================================== main
def main():
    ap = argparse.ArgumentParser(description="count hexagonally packed round filters in a photo")
    ap.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    ap.add_argument("--manual", action="store_true", help="click the 4 outer corners of the tube area first")
    ap.add_argument("--nogui", action="store_true", help="no review window, only save the result picture")
    ap.add_argument("--debug", action="store_true", help="also save count_debug.jpg (shows sites that were rejected)")
    ap.add_argument("--pitch", type=float, help="tube spacing in photo pixels (skips the automatic measurement)")
    ap.add_argument("--diameter", type=float, default=FILTER_DIAMETER_MM, help="filter diameter in mm (for the capacity check)")
    ap.add_argument("--box", type=float, nargs=2, metavar=("W", "H"), help="box inside size in mm (for the capacity check)")
    a = ap.parse_args()

    img = load_image(a.image)
    roi = pick_corners(img) if a.manual else None          # CHANGED: --manual now also works together with --nogui
    try:
        res = analyse(img, a.pitch, roi)
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")

    n = len(res["xy"]); ij = res["ij"]
    rows = np.unique(ij[:, 1]); per_row = [int((ij[:, 1] == j).sum()) for j in rows]
    print(f"image {img.shape[1]}x{img.shape[0]}")
    print(f"  RESULT: {n} filters   ({len(rows)} rows, {min(per_row)}-{max(per_row)} per row, median {int(np.median(per_row))})")
    print(f"  lattice fit error {res['rms']:.2f}px   {100 * res['matched'].mean():.0f}% of the circles sit on a detected tube top, "
          f"the rest were recovered from the lattice")
    if len(res["boxfilled"]):
        print(f"  note: {len(res['boxfilled'])} tubes had no visible detail (over-exposed / glare) and were counted from the lattice position "
              f"- check them with --debug (blue dots)")
    if res["pitch"] < 6: print("  WARNING: tubes are < 6 px apart - the photo resolution is too low for a reliable count.")
    if a.box:
        lb, sb = max(a.box), min(a.box)
        cap = lb * sb / (a.diameter ** 2 * np.sqrt(3) / 2)
        print(f"  theoretical maximum for this box ~ {cap:.0f}   (result = {100 * n / cap:.1f}%)")
        if n > cap:                                         # NEW: over-count check
            print(f"  WARNING: count {n} is ABOVE the physical maximum {cap:.0f} - circles are outside the box. "
                  f"Use --manual, or raise TEX_REL / lower HULL_MARGIN.")

    out = Path(a.image).with_name("count_result.jpg")
    if a.debug:
        dbg = Path(a.image).with_name("count_debug.jpg"); save_image(dbg, render_debug(img, res)); print("saved", dbg)
    if a.nogui:
        save_image(out, render_result(img, res["xy"], numbering(res["ij"], res["xy"]), res["pitch"])); print("saved", out)
    else:
        print("final count:", review(img, res, out))

if __name__ == "__main__":
    main()
