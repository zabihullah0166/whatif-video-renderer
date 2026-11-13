"""What-if video renderer, v2: colour themes, animated backgrounds, 12 scene templates,
word-by-word subtitles, scene transitions and a generated sound bed.

Usage:
  python render.py render script.json     full render -> out/video.mp4
  python render.py sample script.json     contact sheet only (no audio), out/sheet.png

script.json: {"theme": "ocean", "bg": "orbs", "seed": "any text", "scenes": [...]}
(theme, bg and seed are optional: a missing value is picked from the seed.)

Environment:
  ORIENTATION        vertical (1080x1920, default) or landscape (1280x720)
  GOOGLE_TTS_API_KEY Google Cloud Text-to-Speech key. If missing, free Kokoro voice is tried, then offline espeak.
  TTS_VOICE          default en-US-Chirp3-HD-Charon
  TTS_RATE           speaking rate, default 1.0
  KOKORO_VOICE       force one Kokoro voice for every video (default: a different voice per video)
"""
import array, base64, hashlib, json, math, os, random, re, shutil, subprocess, sys, wave

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT_DIR", HERE + "/out")
WORK = OUT + "/work"
PORT = (os.environ.get("ORIENTATION") or "vertical").lower() != "landscape"
W, H = (1080, 1920) if PORT else (1280, 720)
FPS = 30
K = min(W, H) / 720.0
NARR_START = 0.5
TRANS = 0.5

if PORT:
    ART = (W / 2, H * 0.42, W * 0.92, H * 0.60)
    SUB_Y1 = H * 0.80
    SUB_FONT, SUB_MAXCH = 50, 62
else:
    ART = (W / 2, H * 0.40, W * 0.92, H * 0.58)
    SUB_Y1 = H * 0.96
    SUB_FONT, SUB_MAXCH = 36, 62

WHITE, RED = (245, 248, 255), (235, 70, 70)
MUTED = (190, 205, 225)

THEMES = {
    "midnight": dict(top=(12, 20, 46), bot=(14, 62, 74), a1=(255, 140, 60), a2=(60, 200, 190), a3=(245, 197, 66)),
    "aurora": dict(top=(10, 14, 40), bot=(52, 22, 92), a1=(255, 95, 160), a2=(80, 220, 255), a3=(255, 214, 102)),
    "ember": dict(top=(30, 10, 14), bot=(96, 26, 26), a1=(255, 170, 60), a2=(255, 100, 100), a3=(255, 228, 150)),
    "forest": dict(top=(6, 28, 26), bot=(16, 76, 52), a1=(150, 238, 120), a2=(255, 205, 80), a3=(120, 222, 222)),
    "ocean": dict(top=(4, 22, 52), bot=(10, 92, 142), a1=(255, 200, 70), a2=(90, 232, 255), a3=(255, 130, 110)),
    "royal": dict(top=(22, 12, 54), bot=(10, 52, 114), a1=(255, 210, 90), a2=(170, 140, 255), a3=(100, 230, 200)),
    "noir": dict(top=(10, 12, 16), bot=(40, 44, 58), a1=(255, 95, 95), a2=(235, 238, 245), a3=(255, 190, 70)),
    "sunset": dict(top=(42, 14, 62), bot=(128, 42, 64), a1=(255, 190, 80), a2=(255, 124, 164), a3=(130, 222, 255)),
}
BGSTYLES = ["orbs", "bokeh", "grid", "rays"]

# runtime state, filled by setup()
SEED = 0
VAR = 0
SUBSTYLE = 0
TRMODES = ["wipe", "iris", "slide", "zoom"]
THEME = "midnight"
BGSTYLE = "orbs"
PARTS = []
VOICE = "af_heart"
VOICES = ["af_heart", "am_michael", "af_bella", "bm_george", "af_nicole", "am_fenrir", "bf_emma", "af_sarah", "am_puck", "bm_fable", "af_kore", "bf_isabella"]
BG_TOP = BG_BOT = A1 = A2 = A3 = CARD = EDGE = GHOST = (0, 0, 0)
_bg = None


# ---------------- maths ----------------
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ss(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def prog(t, a, b):
    return ss((t - a) / (b - a)) if b > a else 1.0


def lin(t, a, b):
    return clamp((t - a) / (b - a)) if b > a else 1.0


def eoc(p):
    p = clamp(p)
    return 1 - (1 - p) ** 3


def eob(p):
    p = clamp(p)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def lerp(a, b, p):
    return a + (b - a) * p


def mix(c1, c2, p):
    p = clamp(p)
    return tuple(int(lerp(c1[i], c2[i], p)) for i in range(3))


# ---------------- theme and background ----------------
def set_theme(name, style):
    global THEME, BGSTYLE, BG_TOP, BG_BOT, A1, A2, A3, CARD, EDGE, GHOST, _bg
    th = THEMES.get(name) or THEMES["midnight"]
    THEME, BGSTYLE = name if name in THEMES else "midnight", style if style in BGSTYLES else "orbs"
    BG_TOP, BG_BOT, A1, A2, A3 = th["top"], th["bot"], th["a1"], th["a2"], th["a3"]
    CARD = mix(BG_TOP, (255, 255, 255), 0.13)
    EDGE = mix(CARD, A2, 0.65)
    GHOST = mix(BG_TOP, (120, 130, 150), 0.25)
    _bg = Image.new("RGB", (W, H))
    bd = ImageDraw.Draw(_bg)
    for y in range(H):
        bd.line([(0, y), (W, y)], fill=mix(BG_TOP, BG_BOT, y / H))
    vig = Image.radial_gradient("L").resize((W, H), Image.BILINEAR).point(lambda v: int(max(0, v - 90) * 0.55))
    _bg.paste((0, 0, 0), (0, 0), vig)


def bgcol(y):
    return mix(BG_TOP, BG_BOT, clamp(y / H))


def bgat(img, x, y):
    return img.getpixel((int(clamp(x, 0, W - 1)), int(clamp(y, 0, H - 1))))


def fc(img, x, y, col, a):
    return col if a >= 0.999 else mix(bgat(img, x, y), col, a)


def make_particles(rnd):
    return [(rnd.random() * W, rnd.random() * H, (2 + rnd.random() * 4) * K, 14 + rnd.random() * 46, rnd.randrange(3), rnd.random() * 6.28) for _ in range(34)]


def blob_masks(gt):
    ow, oh = W // 4, H // 4
    ma, mb = Image.new("L", (ow, oh), 0), Image.new("L", (ow, oh), 0)
    da, db = ImageDraw.Draw(ma), ImageDraw.Draw(mb)
    st = BGSTYLE
    if st == "orbs":
        for fx, fy, ph, rad, dr, v in ((0.18, 0.11, 0.0, 0.34, da, 70), (0.14, 0.17, 2.1, 0.40, db, 60), (0.22, 0.09, 4.0, 0.28, da, 50)):
            x = ow * (0.5 + 0.42 * math.sin(gt * fx + ph))
            y = oh * (0.5 + 0.42 * math.cos(gt * fy + ph * 1.3))
            r = ow * rad
            dr.ellipse([x - r, y - r, x + r, y + r], fill=v)
        blur = 12
    elif st == "bokeh":
        rnd = random.Random(SEED)
        for i in range(15):
            fx, fy, ph = 0.05 + rnd.random() * 0.12, 0.05 + rnd.random() * 0.1, rnd.random() * 6.28
            x = ow * (0.5 + 0.48 * math.sin(gt * fx + ph))
            y = oh * (0.5 + 0.48 * math.cos(gt * fy + ph * 1.7))
            r = ow * (0.06 + 0.09 * rnd.random())
            (da if i % 2 else db).ellipse([x - r, y - r, x + r, y + r], fill=int(40 + 40 * rnd.random()))
        blur = 2.2
    elif st == "grid":
        for k in range(-1, 15):
            y = (k * oh / 12.0 + gt * 6) % oh
            da.line([(0, y), (ow, y)], fill=60, width=1)
        for k in range(0, 9):
            x = k * ow / 8.0
            da.line([(x, 0), (x, oh)], fill=45, width=1)
        x = ow * (0.5 + 0.35 * math.sin(gt * 0.2))
        y = oh * (0.5 + 0.3 * math.cos(gt * 0.15))
        db.ellipse([x - ow * 0.4, y - ow * 0.4, x + ow * 0.4, y + ow * 0.4], fill=55)
        blur = 0.7
    else:
        for k in range(7):
            a = gt * 0.12 + k * math.pi / 3.5
            spread = 0.20
            pts = [(ow / 2, -oh * 0.12), (ow / 2 + math.cos(a + spread) * oh * 2, -oh * 0.12 + math.sin(a + spread) * oh * 2), (ow / 2 + math.cos(a - spread) * oh * 2, -oh * 0.12 + math.sin(a - spread) * oh * 2)]
            (da if k % 2 else db).polygon(pts, fill=34)
        blur = 3
    return ma.filter(ImageFilter.GaussianBlur(blur)), mb.filter(ImageFilter.GaussianBlur(blur))


def bg_frame(gt):
    img = _bg.copy()
    ma, mb = blob_masks(gt)
    img.paste(A2, (0, 0), ma.resize((W, H), Image.BILINEAR))
    img.paste(A1, (0, 0), mb.resize((W, H), Image.BILINEAR))
    d = ImageDraw.Draw(img)
    cols = (A1, A2, A3)
    for x, y, r, sp, ci, ph in PARTS:
        yy = (y - gt * sp) % H
        a = 0.22 + 0.22 * math.sin(gt * 1.6 + ph)
        d.ellipse([x - r, yy - r, x + r, yy + r], fill=mix(bgcol(yy), cols[ci], a))
    return img


def hud(img, d, idx, n, t, T):
    y = int(H * 0.032)
    hh = max(6, int(7 * K))
    gap = 9 * K
    margin = W * 0.06
    seg = (W - 2 * margin - gap * (n - 1)) / n
    for i in range(n):
        x0 = margin + i * (seg + gap)
        x1 = x0 + seg
        d.rounded_rectangle([x0, y, x1, y + hh], radius=hh // 2, fill=mix(bgcol(y), (255, 255, 255), 0.18))
        f = 1.0 if i < idx else (clamp(t / T) if i == idx else 0.0)
        if f > 0:
            d.rounded_rectangle([x0, y, x0 + max(hh, (x1 - x0) * f), y + hh], radius=hh // 2, fill=A1)


# ---------------- text helpers ----------------
def font(weight, size, _c={}):
    k = (weight, int(size))
    if k not in _c:
        _c[k] = ImageFont.truetype(f"{HERE}/fonts/Poppins-{weight}.ttf", int(size))
    return _c[k]


def wrap(d, s, f, maxw):
    lines, cur = [], ""
    for w in s.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def fit(d, s, weight, size, maxw, minsize=14):
    size = int(size)
    while size > minsize and d.textlength(s, font=font(weight, size)) > maxw:
        size -= 2
    return font(weight, size)


def tx(img, d, x, y, s, weight, size, fill, a=1.0, stroke=0, sfill=None):
    if a <= 0.01:
        return
    kw = {"stroke_width": stroke, "stroke_fill": sfill} if stroke else {}
    d.text((x, y), s, font=font(weight, int(size)), fill=fc(img, x, y, fill, a), anchor="mm", **kw)


def fx(img, d, x, y, s, f, fill, a=1.0):
    if a > 0.01:
        d.text((x, y), s, font=f, fill=fc(img, x, y, fill, a), anchor="mm")


def para(img, d, x, y, s, weight, size, maxw, fill, a=1.0, lh=1.25, maxlines=4):
    """Wrap text and shrink the font until it fits in maxlines. Returns the bottom y."""
    size = int(size)
    while True:
        f = font(weight, size)
        lines = wrap(d, s, f, maxw)
        if len(lines) <= maxlines or size <= 20:
            break
        size -= 4
    for i, l in enumerate(lines):
        fx(img, d, x, y + i * size * lh, l, f, fill, a)
    return y + (len(lines) - 1) * size * lh + size * 0.6


def kin(img, d, x, y, s, weight, size, maxw, fill, t, start, step=0.1, lh=1.18, maxlines=3, hi=(), hicol=None, rise=0.4):
    """Word-by-word pop-in text. Returns the bottom y."""
    size = int(size)
    while True:
        f = font(weight, size)
        lines = wrap(d, s, f, maxw)
        if len(lines) <= maxlines or size <= 20:
            break
        size -= 4
    sp = d.textlength(" ", font=f)
    k = 0
    for li, line in enumerate(lines):
        words = line.split()
        wid = [d.textlength(w, font=f) for w in words]
        wx = x - (sum(wid) + sp * (len(words) - 1)) / 2
        yy = y + li * size * lh
        for w, wd in zip(words, wid):
            e = eob(lin(t, start + k * step, start + k * step + 0.4))
            a = clamp(e * 1.6)
            col = hicol if (hicol and re.sub(r"\W", "", w).lower() in hi) else fill
            if a > 0.01:
                d.text((wx + wd / 2, yy + (1 - e) * size * rise), w, font=f, fill=fc(img, wx + wd / 2, yy, col, a), anchor="mm")
            wx += wd + sp
            k += 1
    return y + (len(lines) - 1) * size * lh + size * 0.6


def hdr(img, d, t, sc, color=None, size=54, maxlines=2):
    cx, cy, aw, ah = ART
    return kin(img, d, cx, cy - ah / 2 + 34 * K, str(sc.get("heading", "")).upper(), "Bold", size * K, aw, color or WHITE, t, 0.3, step=0.07, lh=1.15, maxlines=maxlines)


def card(img, d, x0, y0, x1, y1, fill, edge, rad, w=3, shadow=True):
    if shadow:
        sh = mix(bgat(img, (x0 + x1) / 2, y1), (0, 0, 0), 0.45)
        d.rounded_rectangle([x0 + 5 * K, y0 + 13 * K, x1 + 5 * K, y1 + 13 * K], radius=int(rad), fill=sh)
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(rad), fill=fill, outline=edge, width=max(1, int(w * K)))


def icon_cross(d, cx, cy, r, col, w):
    d.line([(cx - r, cy - r), (cx + r, cy + r)], fill=col, width=int(w))
    d.line([(cx - r, cy + r), (cx + r, cy - r)], fill=col, width=int(w))


def ring(d, cx, cy, r, col, w):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=max(1, int(w)))


def glow_dot(img, d, x, y, r, col, a=1.0):
    d.ellipse([x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2], fill=mix(bgat(img, x, y), col, 0.16 * a))
    d.ellipse([x - r * 1.5, y - r * 1.5, x + r * 1.5, y + r * 1.5], fill=mix(bgat(img, x, y), col, 0.28 * a))
    d.ellipse([x - r, y - r, x + r, y + r], fill=mix(bgat(img, x, y), col, a))


def pal():
    return [A1, A2, A3, mix(A1, A2, 0.5), mix(A2, A3, 0.5)]


def equalizer(img, d, t, y, a=1.0, bars=21, spread=0.7):
    bw = W * spread / (bars * 1.6)
    x0 = W / 2 - (bars * 1.6 * bw) / 2 + bw * 0.3
    for i in range(bars):
        h = (0.22 + 0.78 * abs(math.sin(t * 3.1 + i * 0.7) * math.cos(t * 1.7 + i * 0.31))) * H * (0.065 if PORT else 0.12)
        x = x0 + i * bw * 1.6
        col = mix(A1, A2, i / (bars - 1))
        d.rounded_rectangle([x, y - h / 2, x + bw, y + h / 2], radius=int(bw / 2), fill=fc(img, x, y, col, a))


# ---------------- scene templates ----------------
def sc_title(img, d, t, T, sc):
    cx = W / 2
    qy = H * (0.54 if PORT else 0.52)
    base = int(min(430 * K, H * 0.5) * (1 + 0.03 * math.sin(t * 2.4)) / 6) * 6
    d.text((cx, qy), "?", font=font("Bold", base), fill=mix(bgat(img, cx, qy), A2, 0.10), anchor="mm")
    R = min(W * 0.36, H * 0.34)
    ring(d, cx, qy, R, mix(bgat(img, cx, qy - R), A2, 0.16), 3 * K)
    for i in range(3):
        ang = t * 1.2 + i * 2.094
        glow_dot(img, d, cx + R * math.cos(ang), qy + R * math.sin(ang), 11 * K, (A1, A2, A3)[i], lin(t, 0.4, 0.9))
    y1 = H * (0.21 if PORT else 0.22)
    size = min(168 * K, W * 0.205)
    s = "WHAT IF"
    if VAR == 0:
        p = eob(lin(t, 0.2, 0.75))
        a = clamp(p * 2)
        f = font("Bold", int(size * (1 + (1 - clamp(p)) * 0.8) / 4) * 4)
        fx(img, d, cx + 5 * K, y1 + 9 * K, s, f, (0, 0, 0), a * 0.4)
        fx(img, d, cx, y1, s, f, A1, a)
    else:
        f = font("Bold", int(size))
        total = d.textlength(s, font=f)
        x = cx - total / 2
        for i, ch in enumerate(s):
            w = d.textlength(ch, font=f)
            e = eob(lin(t, 0.2 + i * 0.07, 0.2 + i * 0.07 + 0.42))
            a = clamp(e * 1.8)
            fx(img, d, x + w / 2 + 5 * K, y1 - (1 - e) * H * 0.12 + 9 * K, ch, f, (0, 0, 0), a * 0.4)
            fx(img, d, x + w / 2, y1 - (1 - e) * H * 0.12, ch, f, A1, a)
            x += w
    bw = lin(t, 0.7, 1.3)
    ub = size * 3.3 * eoc(bw)
    if ub > 2:
        d.rounded_rectangle([cx - ub / 2, y1 + size * 0.62, cx + ub / 2, y1 + size * 0.62 + 9 * K], radius=int(5 * K), fill=A3)
    kin(img, d, cx, H * (0.325 if PORT else 0.42), str(sc.get("heading", "")).upper(), "Bold", 70 * K, W * 0.86, WHITE, t, 0.75, step=0.1, lh=1.2, maxlines=3)
    equalizer(img, d, t, H * (0.74 if PORT else 0.86), prog(t, 1.0, 1.6))


def sc_globe(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    hdr(img, d, t, sc, A3)
    r = min(aw * 0.34, ah * 0.30)
    gy = cy + ah * 0.06
    pop = eob(lin(t, 0.2, 0.8))
    r *= max(0.01, pop)
    gl = mix(BG_TOP, A2, 0.30)
    d.ellipse([cx - r * 1.28, gy - r * 1.28, cx + r * 1.28, gy + r * 1.28], fill=mix(bgat(img, cx, gy), A2, 0.08))
    d.ellipse([cx - r * 1.12, gy - r * 1.12, cx + r * 1.12, gy + r * 1.12], fill=mix(bgat(img, cx, gy), A2, 0.13))
    d.ellipse([cx - r, gy - r, cx + r, gy + r], fill=gl, outline=A2, width=max(2, int(5 * K)))
    lc = mix(gl, A2, 0.55)
    for i in range(3):
        w = abs(math.cos(t * 0.7 + i * math.pi / 3))
        d.ellipse([cx - r * w, gy - r, cx + r * w, gy + r], outline=lc, width=2)
    for dy in (-0.55 * r, 0, 0.55 * r):
        h = math.sqrt(max(0.0, r * r - dy * dy))
        d.line([(cx - h, gy + dy), (cx + h, gy + dy)], fill=lc, width=2)
    seed = 7
    for i in range(46):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        a = (seed % 3600) / 3600 * 2 * math.pi
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        rr = math.sqrt((seed % 1000) / 1000) * (r - 14 * K)
        st = 0.7 + i * 0.09
        g = prog(t, st, st + 0.4)
        if g <= 0:
            continue
        rad = (6 + 3 * (i % 3)) * K * g * (1 + 0.25 * math.sin(t * 5 + i))
        glow_dot(img, d, cx + rr * math.cos(a), gy + rr * math.sin(a), rad, A3, g)
    ox, oy = r * 1.45, r * 0.34
    d.ellipse([cx - ox, gy - oy, cx + ox, gy + oy], outline=mix(bgat(img, cx, gy + oy), A1, 0.55), width=max(2, int(3 * K)))
    ang = t * 1.4
    glow_dot(img, d, cx + ox * math.cos(ang), gy + oy * math.sin(ang), 10 * K, A1)
    cap = str(sc.get("caption", "")).upper()
    if cap:
        e = eob(lin(t, 2.0, 2.6))
        capy = gy + r * 1.2 + 60 * K
        if e > 0.02:
            wd = fit(d, cap, "Bold", 44 * K, aw * 0.9)
            tw = d.textlength(cap, font=wd) + 70 * K
            card(img, d, cx - tw / 2 * e, capy - 40 * K, cx + tw / 2 * e, capy + 40 * K, CARD, EDGE, 40 * K, shadow=False)
            fx(img, d, cx, capy, cap, wd, A2, clamp(e))


def sc_gone(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    cw, chh = aw * 0.9, 360 * K * (1 if PORT else 0.75)
    tg = 0.5 * T
    pop = eob(lin(t, 0.3, 0.85))
    p = prog(t, tg - 0.1 * T, tg + 0.1 * T)
    scw, sch = cw * max(0.02, pop) / 2, chh * max(0.02, pop) / 2
    fill = mix(CARD, GHOST, p)
    card(img, d, cx - scw, cy - sch, cx + scw, cy + sch, fill, mix(EDGE, GHOST, p), 36 * K, shadow=p < 0.6)
    shake = math.sin(t * 40) * 4 * K * (1 if tg - 0.15 < t < tg + 0.25 else 0)
    if pop > 0.9:
        kin(img, d, cx + shake, cy - 28 * K, str(sc.get("heading", "")).upper(), "Bold", 66 * K, cw * 0.86, mix(WHITE, (110, 120, 140), p), t, 0.5, step=0.07, lh=1.15, maxlines=3, rise=0.2)
    cap = str(sc.get("caption", ""))
    if cap:
        para(img, d, cx, cy + chh / 2 + 60 * K, cap, "Medium", 36 * K, aw, MUTED, a=prog(t, 0.8, 1.4), maxlines=2)
    s = lin(t, tg, tg + 0.45)
    if s > 0:
        x0, y0, x1, y1 = cx - cw * 0.52, cy + chh * 0.62, cx + cw * 0.52, cy - chh * 0.62
        d.line([(x0, y0), (lerp(x0, x1, eoc(s)), lerp(y0, y1, eoc(s)))], fill=RED, width=int(16 * K))
    tb = tg + 0.3
    if t > tb:
        pp = clamp((t - tb) / 1.1)
        for k in range(18):
            ang = k * 2.399
            v = 0.35 + 0.65 * (((k * 37) % 10) / 10.0)
            dist = eoc(pp) * aw * 0.55 * v
            x = cx + math.cos(ang) * dist
            y = cy + math.sin(ang) * dist + pp * pp * 120 * K
            r = (6 + (k % 3) * 3) * K * (1 - pp)
            if r > 0.5:
                d.rectangle([x - r, y - r, x + r, y + r], fill=mix(bgat(img, x, y), (RED, A1, A3)[k % 3], 1 - pp))
        e = eob(lin(t, tb, tb + 0.5))
        if e > 0.02:
            bx, by, br = cx + cw / 2 - 20 * K, cy - chh / 2 - 6 * K, 46 * K * e
            d.ellipse([bx - br, by - br, bx + br, by + br], fill=RED, outline=WHITE, width=int(4 * K))
            icon_cross(d, bx, by, br * 0.38, WHITE, 7 * K)


def sc_bars(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    panel_e = eob(lin(t, 0.2, 0.7))
    pb = cy + ah * 0.51
    card(img, d, cx - aw / 2, top + (1 - panel_e) * 40 * K, cx + aw / 2, pb, CARD, EDGE, 34 * K, shadow=True)
    cap = 3 if PORT else 4
    L = [str(x)[:10] for x in sc.get("left_items", [])[:cap]]
    R = [str(x)[:10] for x in sc.get("right_items", [])[:cap]]
    n = max(1, len(L) + len(R))
    inner = aw * 0.92
    step = inner / (n + 1)
    bw = min(step * 0.8, 120 * K)
    total = n * step + step
    base = cy + ah * 0.42
    maxh = ah * 0.62
    shrink = prog(t, 0.30 * T, 0.6 * T)
    grow = prog(t, 0.45 * T, 0.85 * T)
    LH, RH = [1.0, 0.85, 0.7, 0.55], [0.5, 0.65, 0.58, 0.8]
    ttop = top + 46 * K
    lw, rw = len(L) * step, len(R) * step
    lx0 = cx - total / 2
    rx0 = lx0 + lw + step
    lt, rt = str(sc.get("left_title", "")).upper(), str(sc.get("right_title", "")).upper()
    fx(img, d, lx0 + lw / 2, ttop, lt, fit(d, lt, "Bold", 38 * K, lw + step * 0.6), A3, prog(t, 0.3, 0.7))
    fx(img, d, rx0 + rw / 2, ttop, rt, fit(d, rt, "Bold", 38 * K, rw + step * 0.6), A2, prog(t, 0.3, 0.7))
    appear = eob(lin(t, 0.4, 1.0))
    for i, lab in enumerate(L):
        bx = lx0 + i * step + (step - bw) / 2
        hh = (10 + maxh * LH[i] * (1 - shrink)) * max(0.02, appear)
        d.rounded_rectangle([bx, base - hh, bx + bw, base], radius=int(10 * K), fill=A3)
        fx(img, d, bx + bw / 2, base + 30 * K, lab, fit(d, lab, "Medium", 24 * K, step * 0.95), MUTED)
    for i, lab in enumerate(R):
        bx = rx0 + i * step + (step - bw) / 2
        hh = 10 + maxh * RH[i] * grow
        d.rounded_rectangle([bx, base - hh, bx + bw, base], radius=int(10 * K), fill=A2)
        fx(img, d, bx + bw / 2, base + 30 * K, lab, fit(d, lab, "Medium", 24 * K, step * 0.95), MUTED)
    d.line([(lx0 - 10, base + 2), (lx0 + total + 10, base + 2)], fill=mix(CARD, WHITE, 0.5), width=3)
    ar = eob(lin(t, 0.3 * T, 0.4 * T))
    if ar > 0.02:
        ax, ay = lx0 + lw + step / 2, base - maxh * 0.5
        s = 22 * K * ar
        d.polygon([(ax - s, ay - s * 0.5), (ax, ay - s * 0.5), (ax, ay - s), (ax + s, ay), (ax, ay + s), (ax, ay + s * 0.5), (ax - s, ay + s * 0.5)], fill=WHITE)


def sc_chips(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    hdr(img, d, t, sc, WHITE)
    items = [str(x) for x in sc.get("items", [])[:5]]
    cols = 2 if PORT else 3
    cw, chh = aw / cols * 0.9, (122 if PORT else 100) * K
    colors = pal()
    rows = max(1, (len(items) + cols - 1) // cols)
    y0 = top + 210 * K
    rowh = 180 * K if PORT else 120 * K
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        in_row = min(cols, len(items) - r * cols)
        x = cx + (c - (in_row - 1) / 2) * (aw / cols)
        y = y0 + r * rowh
        st = 0.8 + i * (0.55 * T / max(1, len(items)))
        e = eob(lin(t, st, st + 0.5))
        if e <= 0.02:
            continue
        w2, h2 = cw / 2 * e, chh / 2 * e
        bob = math.sin(t * 2 + i) * 4 * K if e >= 0.99 else 0
        d.rounded_rectangle([x - w2, y + bob - h2, x + w2, y + bob + h2], radius=int(30 * K), fill=colors[i % 5])
        if e > 0.7:
            fx(img, d, x, y + bob, it.upper(), fit(d, it.upper(), "Bold", 42 * K, cw * 0.86), BG_TOP)
    foot = str(sc.get("footer", ""))
    if foot:
        fy = y0 + rows * rowh + 10 * K
        para(img, d, cx, fy, foot, "Medium", 34 * K, aw, MUTED, a=prog(t, 0.6 * T, 0.75 * T), maxlines=2)


def sc_timeline(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    hdr(img, d, t, sc, WHITE)
    ly = cy - ah * 0.04
    x0, x1 = cx - aw * 0.40, cx + aw * 0.26
    lc = mix(bgat(img, cx, ly), WHITE, 0.4)
    d.line([(x0, ly + 60 * K), (lerp(x0, x1 + 60 * K, eoc(lin(t, 0.3, 1.0))), ly + 60 * K)], fill=lc, width=int(6 * K))
    for x in (x0, x1):
        d.ellipse([x - 13 * K, ly + 47 * K, x + 13 * K, ly + 73 * K], fill=A3)
    for x, key in ((x0, "left_label"), (x1, "right_label")):
        lab = str(sc.get(key, ""))
        lx = min(max(x, 40 + 130 * K), W - 40 - 130 * K)
        para(img, d, lx, ly + 120 * K, lab, "Medium", 30 * K, 260 * K, MUTED, a=prog(t, 0.6, 1.2), lh=1.25, maxlines=3)
    tx_ = x1 + 100 * K
    e = eob(lin(t, 0.4, 1.0))
    for i, r in enumerate((52, 34, 16)):
        rr = r * K * e
        d.ellipse([tx_ - rr, ly + 60 * K - rr, tx_ + rr, ly + 60 * K + rr], fill=RED if i % 2 == 0 else WHITE)
    p = prog(t, 0.15 * T, 0.8 * T)
    mx = lerp(x0, tx_, p)
    my = ly + 24 * K - 130 * K * abs(math.sin(math.pi * 3 * p))
    rr = 30 * K
    glow_dot(img, d, mx, my, rr * 0.8, A1)
    d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=A1, outline=WHITE, width=int(4 * K))


def sc_question(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    kin(img, d, cx, cy - ah / 2 + 60 * K, str(sc.get("heading", "")).upper(), "Bold", 66 * K, aw, WHITE, t, 0.3, step=0.1, lh=1.2, maxlines=3, hicol=A1)
    qy = cy + ah * 0.14
    r = (74 + 6 * math.sin(t * 4)) * K * eob(lin(t, 0.5, 1.1))
    for k in range(3):
        rr = r + (k * 26 + ((t * 40) % 26)) * K * 0.9
        a = 0.35 * (1 - ((k * 26 + (t * 40) % 26) / 78.0))
        if a > 0:
            ring(d, cx, qy, rr, mix(bgat(img, cx, qy - rr), A2, a), 3 * K)
    ring(d, cx, qy, r, A2, 7 * K)
    fx(img, d, cx, qy - 4 * K, "?", font("Bold", int(110 * K)), A2, clamp(r / (74 * K)))
    pe = eob(lin(t, 1.0, 1.6))
    if pe > 0.02:
        py = cy + ah * 0.40
        pw, ph = aw * 0.8 * pe / 2, 62 * K * pe
        pulse = 1 + 0.02 * math.sin(t * 5)
        d.rounded_rectangle([cx - pw * pulse, py - ph, cx + pw * pulse, py + ph], radius=int(ph), fill=A1)
        if pe > 0.8:
            fx(img, d, cx, py, "COMMENT YOUR ANSWER", fit(d, "COMMENT YOUR ANSWER", "Bold", 38 * K, aw * 0.72), BG_TOP)
            ay = py - ph - 36 * K + abs(math.sin(t * 4)) * 12 * K
            d.polygon([(cx - 22 * K, ay - 10 * K), (cx + 22 * K, ay - 10 * K), (cx, ay + 18 * K)], fill=A3)


def sc_stat(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    hdr(img, d, t, sc, A3, size=46)
    val = str(sc.get("value", "") or "").strip()
    m = re.match(r"^\s*([0-9][0-9.,]*)(.*)$", val)
    cy2 = cy + ah * 0.04
    frac = None
    if m:
        raw = m.group(1).replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            num = None
    else:
        num = None
    if num is not None:
        dec = len(raw.split(".")[1]) if "." in raw else 0
        shown = num * eoc(lin(t, 0.4, 1.8))
        text = (f"{shown:,.{dec}f}" if dec else f"{int(round(shown)):,}") + m.group(2)
        if "%" in m.group(2) and num <= 100:
            frac = num / 100.0 * eoc(lin(t, 0.4, 1.8))
        scale = 1.0
    else:
        text = val or "?"
        scale = 0.6 + 0.4 * eob(lin(t, 0.4, 1.0))
    rad = min(aw * 0.40, ah * 0.30)
    ring(d, cx, cy2, rad, mix(bgat(img, cx, cy2 - rad), WHITE, 0.14), 18 * K)
    if frac is not None and frac > 0.003:
        d.arc([cx - rad, cy2 - rad, cx + rad, cy2 + rad], -90, -90 + 360 * frac, fill=A1, width=int(18 * K))
    else:
        for k in range(3):
            rr = rad * (0.65 + 0.12 * k) + math.sin(t * 2 + k) * 6 * K
            ring(d, cx, cy2, rr, mix(bgat(img, cx, cy2 - rr), A2, 0.12 + 0.06 * (2 - k)), 3 * K)
    f = fit(d, text, "Bold", 190 * K * scale, rad * 1.6)
    fx(img, d, cx + 4 * K, cy2 + 8 * K, text, f, (0, 0, 0), 0.35)
    fx(img, d, cx, cy2, text, f, WHITE)
    lab = str(sc.get("caption", "") or sc.get("label", ""))
    if lab:
        para(img, d, cx, cy2 + rad + 70 * K, lab, "Medium", 40 * K, aw * 0.92, MUTED, a=prog(t, 1.2, 1.8), maxlines=2)


def sc_versus(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    gapv = 120 * K
    if PORT:
        cw, chh = aw, (ah - gapv) / 2
        boxes = [(cx - cw / 2, top, cx + cw / 2, top + chh), (cx - cw / 2, top + chh + gapv, cx + cw / 2, top + 2 * chh + gapv)]
        vs = (cx, top + chh + gapv / 2)
    else:
        cw, chh = (aw - gapv) / 2, ah
        boxes = [(cx - aw / 2, top, cx - aw / 2 + cw, top + chh), (cx + aw / 2 - cw, top, cx + aw / 2, top + chh)]
        vs = (cx, cy)
    sides = [("left_title", "left_items", A3, -1), ("right_title", "right_items", A2, 1)]
    for bi, (tk, ik, col, sgn) in enumerate(sides):
        e = eoc(lin(t, 0.2 + bi * 0.25, 0.8 + bi * 0.25))
        off = (1 - e) * W * sgn * (0.0 if PORT else 1.0)
        offy = (1 - e) * H * 0.25 * (1 if bi else -1) if PORT else 0
        x0, y0, x1, y1 = boxes[bi]
        x0, x1, y0, y1 = x0 + off, x1 + off, y0 + offy, y1 + offy
        card(img, d, x0, y0, x1, y1, CARD, col, 34 * K, w=4)
        d.rounded_rectangle([x0 + 28 * K, y0 + 22 * K, x0 + 28 * K + 14 * K, y0 + 22 * K + 52 * K], radius=int(6 * K), fill=col)
        ttl = str(sc.get(tk, "")).upper()
        d.text((x0 + 62 * K, y0 + 48 * K), ttl, font=fit(d, ttl, "Bold", 46 * K, cw - 110 * K), fill=col, anchor="lm")
        items = [str(x) for x in sc.get(ik, [])[:3]]
        for k, it in enumerate(items):
            a = eob(lin(t, 0.9 + bi * 0.5 + k * 0.35, 1.3 + bi * 0.5 + k * 0.35))
            if a <= 0.02:
                continue
            yy = y0 + 135 * K + k * 66 * K if PORT else y0 + 120 * K + k * 60 * K
            glow_dot(img, d, x0 + 52 * K, yy, 9 * K * min(1, a), col)
            fx(img, d, x0 + 84 * K, yy, it, font("Medium", int(min(40 * K, 38 * K))), WHITE, clamp(a)) if False else d.text((x0 + 84 * K, yy + (1 - clamp(a)) * 18 * K), it, font=fit(d, it, "Medium", 40 * K, cw - 130 * K), fill=fc(img, x0 + 84 * K, yy, WHITE, clamp(a)), anchor="lm")
    pe = eob(lin(t, 0.7, 1.2))
    if pe > 0.02:
        r = 52 * K * pe
        d.ellipse([vs[0] - r, vs[1] - r, vs[0] + r, vs[1] + r], fill=A1, outline=WHITE, width=int(5 * K))
        if pe > 0.7:
            fx(img, d, vs[0], vs[1], "VS", font("Bold", int(44 * K)), BG_TOP)


def sc_chain(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    hdr(img, d, t, sc, WHITE, size=50)
    items = [str(x)[:24] for x in sc.get("items", [])[:4]] or [""]
    n = len(items)
    y0 = top + 170 * K
    avail = ah - 190 * K
    nh = min(150 * K, avail / (n + (n - 1) * 0.6))
    gap = nh * 0.6
    colors = pal()
    nw = aw * 0.86
    for i, it in enumerate(items):
        st = 0.7 + i * (0.5 * T / n)
        e = eob(lin(t, st, st + 0.45))
        y = y0 + i * (nh + gap) + nh / 2
        if i > 0:
            f = lin(t, st - 0.3, st + 0.05)
            yb = y - nh / 2 - gap
            if f > 0:
                d.line([(cx, yb), (cx, yb + gap * f)], fill=colors[i - 1], width=int(7 * K))
                if f >= 1:
                    d.polygon([(cx - 14 * K, yb + gap - 14 * K), (cx + 14 * K, yb + gap - 14 * K), (cx, yb + gap + 2 * K)], fill=colors[i - 1])
        if e <= 0.02:
            continue
        w2, h2 = nw / 2 * e, nh / 2 * e
        active = (t >= st) and (i == n - 1 or t < 0.7 + (i + 1) * (0.5 * T / n))
        ew = 3 + (4 * abs(math.sin(t * 5)) if active else 0)
        card(img, d, cx - w2, y - h2, cx + w2, y + h2, CARD, colors[i], 28 * K, w=ew)
        if e > 0.8:
            rr = nh * 0.32
            nx = cx - nw / 2 + nh * 0.5
            d.ellipse([nx - rr, y - rr, nx + rr, y + rr], fill=colors[i])
            fx(img, d, nx, y, str(i + 1), font("Bold", int(rr * 1.2)), BG_TOP)
            fx(img, d, cx + nh * 0.25, y, it, fit(d, it, "Bold", 38 * K, nw - nh * 1.4), WHITE)


def sc_steps(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    hdr(img, d, t, sc, A3, size=50)
    items = [str(x)[:26] for x in sc.get("items", [])[:4]] or [""]
    n = len(items)
    y0 = top + 170 * K
    avail = ah - 190 * K
    rh = min(180 * K, avail / n)
    colors = pal()
    left = cx - aw / 2 + 10 * K
    for i, it in enumerate(items):
        st = 0.7 + i * (0.5 * T / n)
        e = eoc(lin(t, st, st + 0.5))
        y = y0 + i * rh + rh / 2
        if e <= 0.02:
            continue
        off = (1 - e) * W * 0.4
        num = f"{i + 1:02d}"
        d.text((left + off, y), num, font=font("Bold", int(min(84 * K, rh * 0.62))), fill=fc(img, left, y, colors[i], e), anchor="lm")
        tx_ = left + 140 * K + off
        d.text((tx_, y), it, font=fit(d, it, "Bold", 40 * K, aw - 160 * K), fill=fc(img, tx_, y, WHITE, e), anchor="lm")
        ln = lin(t, st + 0.2, st + 0.7)
        d.line([(left, y + rh / 2 - 6 * K), (left + (aw - 20 * K) * ln, y + rh / 2 - 6 * K)], fill=mix(bgat(img, cx, y), WHITE, 0.18), width=max(2, int(3 * K)))


def sc_fact(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    lab = str(sc.get("label", "") or "DID YOU KNOW?").upper()
    pe = eob(lin(t, 0.2, 0.7))
    if pe > 0.02:
        lf = font("Bold", int(34 * K))
        pw = (d.textlength(lab, font=lf) + 80 * K) * pe / 2
        py = cy - ah * 0.36
        d.rounded_rectangle([cx - pw, py - 36 * K * pe, cx + pw, py + 36 * K * pe], radius=int(36 * K), fill=A2)
        if pe > 0.8:
            d.text((cx, py), lab, font=lf, fill=BG_TOP, anchor="mm")
    hi = set(re.sub(r"\W", "", w).lower() for w in str(sc.get("highlight", "")).split())
    bottom = kin(img, d, cx, cy - ah * 0.22, str(sc.get("heading", "")).upper(), "Bold", 96 * K, aw * 0.94, WHITE, t, 0.3, step=0.12, lh=1.14, maxlines=5, hi=hi, hicol=A1)
    bw = aw * 0.34 * eoc(lin(t, 1.0, 1.7))
    d.rounded_rectangle([cx - bw / 2, bottom + 40 * K, cx + bw / 2, bottom + 40 * K + 10 * K], radius=int(5 * K), fill=A3)
    cap = str(sc.get("caption", ""))
    if cap:
        para(img, d, cx, bottom + 110 * K, cap, "Medium", 38 * K, aw * 0.9, MUTED, a=prog(t, 1.4, 2.0), maxlines=2)


FN = {"title": sc_title, "globe": sc_globe, "gone": sc_gone, "bars": sc_bars, "chips": sc_chips, "timeline": sc_timeline, "question": sc_question,
      "stat": sc_stat, "versus": sc_versus, "chain": sc_chain, "steps": sc_steps, "fact": sc_fact}


# ---------------- subtitles ----------------
def chunks(text):
    out = []

    def split(s):
        s = s.strip()
        if len(s) <= SUB_MAXCH:
            return [s]
        cands = [m.start() for m in re.finditer(r"[,;:]| and | but | or ", s)]
        if not cands:
            k = s.rfind(" ", 0, len(s) // 2 + 6)
            if k <= 0:
                return [s]
            return split(s[:k]) + split(s[k:])
        k = min(cands, key=lambda i: abs(i - len(s) / 2))
        k = k + 1 if s[k] in ",;:" else k
        return split(s[:k]) + split(s[k:])

    for s in re.findall(r"[^.?!]+[.?!]?", text):
        if s.strip():
            out += split(s)
    return [c for c in out if c.strip()] or [text]


def subtitle(img, d, sc, t, narr):
    """Karaoke style: the word being spoken is highlighted."""
    if sc["type"] == "title":
        return
    ch = chunks(sc["narration"])
    total = sum(len(c) for c in ch) or 1
    tt = t - NARR_START
    if tt < -0.05 or tt > narr + 0.5:
        return
    acc, cur, c_start, c_end = 0, ch[-1], 0.0, narr
    for c in ch:
        c_start, c_end = acc / total * narr, (acc + len(c)) / total * narr
        if tt <= c_end + 0.02:
            cur = c
            break
        acc += len(c)
    cur = cur.strip().rstrip(",;:")
    words = cur.split()
    if not words:
        return
    pw = clamp((tt - c_start) / max(0.05, c_end - c_start))
    cum, hot = 0, len(words) - 1
    nchar = sum(len(w) for w in words) or 1
    for i, w in enumerate(words):
        cum += len(w)
        if pw <= cum / nchar + 0.001:
            hot = i
            break
    f = font("Medium", SUB_FONT)
    lines = wrap(d, cur, f, W * 0.88)
    lh = SUB_FONT * 1.38
    bh = lh * len(lines) + 28
    y0 = SUB_Y1 - bh
    sp = d.textlength(" ", font=f)
    wmax = max(d.textlength(l, font=f) for l in lines)
    if SUBSTYLE == 0:
        d.rounded_rectangle([W / 2 - wmax / 2 - 30, y0, W / 2 + wmax / 2 + 30, SUB_Y1], radius=22, fill=(6, 10, 24))
    k = 0
    for li, line in enumerate(lines):
        lw = d.textlength(line, font=f)
        x = W / 2 - lw / 2
        y = y0 + 14 + lh * li + lh / 2 - 2
        for w in line.split():
            wd = d.textlength(w, font=f)
            col = A3 if k == hot else WHITE
            if SUBSTYLE == 1:
                d.text((x + wd / 2, y), w, font=f, fill=col, anchor="mm", stroke_width=max(4, int(SUB_FONT * 0.14)), stroke_fill=(6, 10, 24))
            else:
                d.text((x + wd / 2, y), w, font=f, fill=col, anchor="mm")
            x += wd + sp
            k += 1


# ---------------- frame composition ----------------
def transition(content, bgonly, t, idx):
    p = clamp(t / TRANS)
    mode = TRMODES[idx % len(TRMODES)]
    if mode == "wipe":
        sl = (H if PORT else W) * 0.12
        out = (bgonly if p < 0.5 else content).copy()
        dd = ImageDraw.Draw(out)
        if p < 0.5:
            e = lerp(-sl, W + sl, p * 2)
            dd.polygon([(0, 0), (e + sl, 0), (e, H), (0, H)], fill=A1)
            dd.line([(e + sl, 0), (e, H)], fill=WHITE, width=int(9 * K))
        else:
            e = lerp(-sl, W + sl, (p - 0.5) * 2)
            dd.polygon([(e + sl, 0), (W, 0), (W, H), (e, H)], fill=A1)
            dd.line([(e + sl, 0), (e, H)], fill=WHITE, width=int(9 * K))
        return out
    if mode == "iris":
        out = bgonly.copy()
        r = eoc(p) * math.hypot(W, H) * 0.55
        cx, cy = W / 2, H * 0.42
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        out.paste(content, (0, 0), m)
        if p < 0.98:
            ImageDraw.Draw(out).ellipse([cx - r, cy - r, cx + r, cy + r], outline=A1, width=int(8 * K))
        return out
    if mode == "slide":
        out = bgonly.copy()
        e = eoc(p)
        out.paste(Image.blend(bgonly, content, e), (0, int((1 - e) * H * 0.10)))
        return out
    e = eoc(p)
    s = 1.18 - 0.18 * e
    sz = (int(W * s), int(H * s))
    z = content.resize(sz, Image.BILINEAR)
    l, tp = (sz[0] - W) // 2, (sz[1] - H) // 2
    return Image.blend(bgonly, z.crop((l, tp, l + W, tp + H)), e)


def frame(sc, t, T, narr):
    idx, n, gt = sc["_i"], sc["_n"], sc["_g"] + t
    base = bg_frame(gt)
    hud(base, ImageDraw.Draw(base), idx, n, t, T)
    img = base.copy()
    d = ImageDraw.Draw(img)
    FN.get(sc["type"], sc_fact)(img, d, t, T, sc)
    subtitle(img, d, sc, t, narr)
    if t < TRANS:
        img = transition(img, base, t, idx)
    if idx == n - 1 and T - t < 0.5:
        img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), clamp(1 - (T - t) / 0.5))
    return img
# ---------------- audio ----------------
def clean(s):
    return re.sub(r"\s+", " ", str(s).replace("—", ", ").replace("–", ", ")).strip()


def tts_google(text, path):
    key = os.environ.get("GOOGLE_TTS_API_KEY", "").strip()
    if not key:
        return False
    import requests
    voice = os.environ.get("TTS_VOICE") or "en-US-Chirp3-HD-Charon"
    lang = "-".join(voice.split("-")[:2])
    body = {"input": {"text": text}, "voice": {"languageCode": lang, "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "speakingRate": float(os.environ.get("TTS_RATE") or 1.0)}}
    try:
        r = requests.post("https://texttospeech.googleapis.com/v1/text:synthesize", params={"key": key}, json=body, timeout=60)
    except Exception as e:
        print("Google TTS error:", e)
        return False
    if r.status_code != 200:
        print("Google TTS failed:", r.status_code, r.text[:300])
        return False
    open(path, "wb").write(base64.b64decode(r.json()["audioContent"]))
    return True


_KPIPES = {}


def tts_kokoro(text, path):
    """Free, no-key, natural voice (Kokoro-82M, Apache-2.0) running on the CPU."""
    if os.environ.get("TTS_PROVIDER", "kokoro").lower() != "kokoro":
        return False
    import numpy as np, soundfile as sf
    speed = float(os.environ.get("TTS_RATE") or 1.0)
    for voice in (VOICE, "af_heart"):
        try:
            lang = "b" if voice[:1] == "b" else "a"
            if lang not in _KPIPES:
                from kokoro import KPipeline
                _KPIPES[lang] = KPipeline(lang_code=lang)
            parts = [np.asarray(a) for _, _, a in _KPIPES[lang](text, voice=voice, speed=speed) if a is not None]
            if parts:
                sf.write(path, np.concatenate(parts), 24000)
                return True
        except Exception as e:
            print("Kokoro voice", voice, "failed:", repr(e))
    return False


def tts_espeak(text, path):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe:
        with open(path, "wb") as f:
            subprocess.run([exe, "--stdout", "-s", "150", "-v", "en-us", text], stdout=f, check=True)
        return True
    import ctypes
    import espeakng_loader as e
    global _ESP
    if _ESP is None:
        lib = ctypes.CDLL(e.get_library_path())
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
        buf = array.array("h")

        def cb(wav, n, ev):
            if wav and n > 0:
                buf.extend(wav[:n])
            return 0

        cbf = CB(cb)
        rate = lib.espeak_Initialize(1, 0, os.path.dirname(e.get_data_path()).encode(), 0)
        lib.espeak_SetSynthCallback(cbf)
        lib.espeak_SetVoiceByName(b"en-us")
        lib.espeak_SetParameter(1, 145, 0)
        lib.espeak_SetParameter(3, 38, 0)
        _ESP = (lib, cbf, buf, rate)
    lib, cbf, buf, rate = _ESP
    del buf[:]
    b = text.encode()
    lib.espeak_Synth(b, len(b) + 1, 0, 0, 0, 1, None, None)
    lib.espeak_Synchronize()
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(buf.tobytes())
    return True


_ESP = None


def make_voice(text, idx):
    raw, norm = f"{WORK}/raw_{idx}.wav", f"{WORK}/n_{idx}.wav"
    if not (tts_google(text, raw) or tts_kokoro(text, raw)):
        print("Using offline voice for scene", idx)
        tts_espeak(text, raw)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", norm], check=True)
    with wave.open(norm) as w:
        return norm, w.getnframes() / w.getframerate()


# ---------------- sound bed ----------------
def sound_bed(total, starts, seed, sr=24000):
    """Quiet ambient pad plus a soft whoosh on every scene change. Returns float samples."""
    import numpy as np
    n = int((total + 1.0) * sr)
    tt = np.arange(n) / sr
    roots = [110.0, 98.0, 82.41, 92.5, 103.83]
    root = roots[seed % len(roots)]
    chords = [[1, 1.2, 1.5, 2.0, 3.0], [0.89, 1.12, 1.335, 1.78, 2.67], [0.84, 1.0, 1.26, 1.68, 2.52]]
    bed = np.zeros(n)
    seg = 6.0
    for ci in range(int(total / seg) + 2):
        ch = chords[(ci + seed) % len(chords)]
        env = np.clip(1 - np.abs((tt - ci * seg - seg / 2) / (seg * 0.85)), 0, 1) ** 1.3
        for k, r in enumerate(ch):
            f = root * r
            lfo = 0.75 + 0.25 * np.sin(2 * np.pi * (0.07 + 0.02 * k) * tt + k)
            bed += env * lfo * (np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * f * 2.003 * tt)) / (1 + 0.4 * k)
    bed *= 0.05 / max(1e-6, np.abs(bed).max())
    fade = np.clip(np.minimum(tt / 2.0, (total + 0.6 - tt) / 1.5), 0, 1)
    bed *= fade
    rs = np.random.RandomState(seed % 100000)
    wn = int(0.6 * sr)
    noise = rs.randn(wn)
    y = np.empty(wn)
    acc = 0.0
    for i in range(wn):
        c = 0.02 + 0.45 * (i / wn)
        acc += c * (noise[i] - acc)
        y[i] = acc
    y *= np.sin(np.pi * np.linspace(0, 1, wn)) ** 2
    y *= 0.16 / max(1e-6, np.abs(y).max())
    for s in starts:
        a = int((s + 0.02) * sr)
        if a < n:
            e = min(n, a + wn)
            bed[a:e] += y[: e - a]
    return bed


# ---------------- build ----------------
def setup(meta):
    global SEED, VAR, SUBSTYLE, TRMODES, PARTS, VOICE
    src = str(meta.get("seed") or meta.get("title") or meta.get("topic") or "whatif")
    SEED = int(hashlib.md5(src.encode()).hexdigest()[:8], 16)
    names = sorted(THEMES)
    theme = meta.get("theme") if meta.get("theme") in THEMES else names[SEED % len(names)]
    style = meta.get("bg") if meta.get("bg") in BGSTYLES else BGSTYLES[(SEED >> 3) % len(BGSTYLES)]
    VAR = (SEED >> 5) % 2
    SUBSTYLE = (SEED >> 7) % 2
    modes = ["wipe", "iris", "slide", "zoom"]
    random.Random(SEED).shuffle(modes)
    TRMODES = modes
    PARTS = make_particles(random.Random(SEED + 1))
    forced = (os.environ.get("KOKORO_VOICE") or "").strip()
    pick = meta.get("voice") if meta.get("voice") in VOICES else VOICES[(SEED >> 9) % len(VOICES)]
    VOICE = forced or pick
    set_theme(theme, style)
    print("look:", THEME, BGSTYLE, "voice", VOICE, "title", VAR, "subs", SUBSTYLE, "transitions", TRMODES, flush=True)


def load(path):
    data = json.load(open(path))
    meta = {} if isinstance(data, list) else {k: v for k, v in data.items() if k != "scenes"}
    sc = data if isinstance(data, list) else data["scenes"]
    for s in sc:
        s["narration"] = clean(s.get("narration", ""))
        s["type"] = s.get("type", "fact")
    return sc, meta


def annotate(scenes, starts=None):
    for i, s in enumerate(scenes):
        s["_i"], s["_n"], s["_g"] = i, len(scenes), (starts[i] if starts else 0.0)


def sample(scenes):
    os.makedirs(OUT, exist_ok=True)
    annotate(scenes)
    n = len(scenes)
    cols = 4 if PORT else 3
    th = (W // 3, H // 3) if PORT else (426, 240)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (th[0] * cols, th[1] * rows))
    for i, sc in enumerate(scenes):
        T = 9.0
        im = frame(sc, T * 0.8, T, 6.0).resize(th, Image.LANCZOS)
        sheet.paste(im, ((i % cols) * th[0], (i // cols) * th[1]))
    sheet.save(OUT + "/sheet.png")
    print("sheet saved", sheet.size)


def render(scenes):
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(WORK)
    info = []
    t0 = 0.0
    starts = []
    for i, sc in enumerate(scenes):
        wav, n = make_voice(sc["narration"], i)
        T = NARR_START + n + (1.0 if sc["type"] == "title" else 0.7)
        T = math.ceil(T * FPS) / FPS
        info.append((sc, wav, n, T))
        starts.append(t0)
        t0 += T
    annotate(scenes, starts)
    with open(WORK + "/list.txt", "w") as lst:
        for i, (sc, wav, n, T) in enumerate(info):
            out, tmp = f"{WORK}/s{i}.mp4", f"{WORK}/s{i}.part.mp4"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                   "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", tmp]
            pr = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            for k in range(int(round(T * FPS))):
                pr.stdin.write(frame(sc, k / FPS, T, n).tobytes())
            pr.stdin.close()
            if pr.wait() != 0:
                raise SystemExit(f"ffmpeg failed on scene {i}")
            os.rename(tmp, out)
            lst.write(f"file '{out}'\n")
            print("scene", i, sc["type"], round(T, 1), "s", flush=True)
    sr = 24000
    allp = array.array("h")
    for sc, wav, n, T in info:
        with wave.open(wav) as w:
            s = array.array("h", w.readframes(w.getnframes()))
        seg = array.array("h", [0] * int(NARR_START * sr)) + s
        seg.extend([0] * (int(T * sr) - len(seg)))
        allp.extend(seg[: int(T * sr)])
    try:
        import numpy as np
        voice = np.frombuffer(allp.tobytes(), dtype=np.int16).astype(np.float64)
        bed = sound_bed(len(voice) / sr, starts, SEED, sr)[: len(voice)] * 32767
        mixed = np.clip(voice + bed, -32767, 32767).astype(np.int16)
        pcm = mixed.tobytes()
    except Exception as e:
        print("Sound bed skipped:", repr(e))
        pcm = allp.tobytes()
    with wave.open(WORK + "/full.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm)
    final_tmp = OUT + "/video.part.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", WORK + "/list.txt", "-i", WORK + "/full.wav",
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", final_tmp], check=True)
    os.rename(final_tmp, OUT + "/video.mp4")
    print("done", OUT + "/video.mp4", round(sum(x[3] for x in info), 1), "s")


if __name__ == "__main__":
    mode, path = sys.argv[1], sys.argv[2]
    scenes, meta = load(path)
    setup(meta)
    {"render": render, "sample": sample}[mode](scenes)
