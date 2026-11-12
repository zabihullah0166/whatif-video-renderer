"""What-if video renderer.

Usage:
  python render.py render script.json     full render -> out/video.mp4
  python render.py sample script.json     contact sheet only (no audio), out/sheet.png

Environment:
  ORIENTATION        vertical (1080x1920, default) or landscape (1280x720)
  GOOGLE_TTS_API_KEY Google Cloud Text-to-Speech key. If missing or failing, an offline voice is used.
  TTS_VOICE          default en-US-Chirp3-HD-Charon
  TTS_RATE           speaking rate, default 1.0
"""
import array, base64, json, math, os, re, shutil, subprocess, sys, wave

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT_DIR", HERE + "/out")
WORK = OUT + "/work"
PORT = (os.environ.get("ORIENTATION") or "vertical").lower() != "landscape"
W, H = (1080, 1920) if PORT else (1280, 720)
FPS = 30
K = min(W, H) / 720.0
NARR_START = 0.35

if PORT:
    ART = (W / 2, H * 0.38, W * 0.92, H * 0.54)
    SUB_Y1 = H * 0.80
    SUB_FONT, SUB_MAXCH = int(50), 62
else:
    ART = (W / 2, H * 0.40, W * 0.92, H * 0.56)
    SUB_Y1 = H * 0.96
    SUB_FONT, SUB_MAXCH = 36, 62

GOLD, TEAL, ORANGE, WHITE = (245, 197, 66), (60, 200, 190), (255, 140, 60), (245, 248, 255)
BG_TOP, BG_BOT = (12, 20, 46), (14, 62, 74)


def font(weight, size, _c={}):
    k = (weight, int(size))
    if k not in _c:
        _c[k] = ImageFont.truetype(f"{HERE}/fonts/Poppins-{weight}.ttf", int(size))
    return _c[k]


def ss(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def prog(t, a, b):
    return ss((t - a) / (b - a)) if b > a else 1.0


def lerp(a, b, p):
    return a + (b - a) * p


def mix(c1, c2, p):
    return tuple(int(lerp(c1[i], c2[i], p)) for i in range(3))


_bg = Image.new("RGB", (W, H))
_bd = ImageDraw.Draw(_bg)
for _y in range(H):
    _bd.line([(0, _y), (W, _y)], fill=mix(BG_TOP, BG_BOT, _y / H))


def bgcol(y):
    return mix(BG_TOP, BG_BOT, max(0, min(H - 1, y)) / H)


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


def ctext(d, x, y, s, f, fill):
    d.text((x, y), s, font=f, fill=fill, anchor="mm")


def para(d, x, y, s, weight, size, maxw, fill, lh=1.25, maxlines=4):
    """Wrap text and shrink the font until it fits in maxlines. Returns the bottom y."""
    size = int(size)
    while True:
        f = font(weight, size)
        lines = wrap(d, s, f, maxw)
        if len(lines) <= maxlines or size <= 20:
            break
        size -= 4
    for i, l in enumerate(lines):
        ctext(d, x, y + i * size * lh, l, f, fill)
    return y + (len(lines) - 1) * size * lh + size * 0.6


def ring(d, cx, cy, r, col, w):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=int(w))


# ---------------- scene templates ----------------
def sc_title(img, d, t, T, sc):
    p = prog(t, 0.0, 0.5)
    y1 = H * (0.20 if PORT else 0.22)
    ctext(d, W / 2, y1, "WHAT IF", font("Bold", min(150 * K, W * 0.2)), mix(bgcol(y1), ORANGE, p))
    p2 = prog(t, 0.4, 0.9)
    y2 = H * (0.33 if PORT else 0.40)
    para(d, W / 2, y2, sc.get("heading", "").upper(), "Bold", 64 * K, W * 0.86, mix(bgcol(y2), WHITE, p2), lh=1.2, maxlines=3)
    cy = H * (0.70 if PORT else 0.82)
    for i in range(3):
        r = (44 + i * 30 + 8 * math.sin(t * 3 - i)) * K
        ring(d, W / 2, cy, r, mix(bgcol(cy), ORANGE, 0.9 - i * 0.25), 5 * K)


def sc_globe(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    hy = top + 28 * K
    para(d, cx, hy, sc.get("heading", "").upper(), "Bold", 52 * K, aw, mix(bgcol(hy), GOLD, prog(t, 0.3, 0.9)), maxlines=2)
    r = min(aw * 0.36, ah * 0.32)
    gy = cy + ah * 0.04
    d.ellipse([cx - r, gy - r, cx + r, gy + r], fill=(20, 72, 116), outline=(110, 190, 235), width=int(5 * K))
    lc = (70, 140, 190)
    for k in (0.35, 0.7):
        d.ellipse([cx - r * k, gy - r, cx + r * k, gy + r], outline=lc, width=2)
    for dy in (-0.55 * r, 0, 0.55 * r):
        h = math.sqrt(r * r - dy * dy)
        d.line([(cx - h, gy + dy), (cx + h, gy + dy)], fill=lc, width=2)
    d.line([(cx, gy - r), (cx, gy + r)], fill=lc, width=2)
    seed = 7
    for i in range(46):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        a = (seed % 3600) / 3600 * 2 * math.pi
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        rr = math.sqrt((seed % 1000) / 1000) * (r - 14 * K)
        st = 0.5 + i * 0.09
        g = prog(t, st, st + 0.4)
        if g <= 0:
            continue
        rad = (7 + 3 * (i % 3)) * K * g * (1 + 0.25 * math.sin(t * 5 + i))
        x, y = cx + rr * math.cos(a), gy + rr * math.sin(a)
        d.ellipse([x - rad * 1.9, y - rad * 1.9, x + rad * 1.9, y + rad * 1.9], fill=mix((20, 72, 116), (120, 100, 40), g * 0.6))
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 214, 90))
    cap = sc.get("caption", "").upper()
    if cap:
        capy = gy + r + 55 * K
        para(d, cx, capy, cap, "Bold", 44 * K, aw, mix(bgcol(capy), TEAL, prog(t, 2.0, 2.6)), maxlines=2)


def sc_gone(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    cw, chh = aw * 0.9, 260 * K
    p = prog(t, 0.45 * T, 0.65 * T)
    card = mix((30, 56, 92), (28, 36, 52), p)
    d.rounded_rectangle([cx - cw / 2, cy - chh / 2, cx + cw / 2, cy + chh / 2], radius=int(36 * K), fill=card, outline=mix((90, 140, 200), (70, 78, 96), p), width=int(4 * K))
    shake = math.sin(t * 40) * 3 * K * (1 if 0.4 * T < t < 0.6 * T else 0)
    para(d, cx + shake, cy - 22 * K, sc.get("heading", "").upper(), "Bold", 60 * K, cw * 0.88, mix(WHITE, (110, 120, 140), p), lh=1.15, maxlines=3)
    cap = sc.get("caption", "")
    if cap:
        capy = cy + chh / 2 + 55 * K
        para(d, cx, capy, cap, "Medium", 34 * K, aw, mix(bgcol(capy), (170, 200, 230), prog(t, 0.6, 1.2)), maxlines=2)
    s = prog(t, 0.55 * T, 0.55 * T + 0.7)
    if s > 0:
        x0, y0, x1, y1 = cx - cw * 0.52, cy + chh * 0.8, cx + cw * 0.52, cy - chh * 0.8
        d.line([(x0, y0), (lerp(x0, x1, s), lerp(y0, y1, s))], fill=(235, 70, 70), width=int(16 * K))


def sc_bars(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    cap = 3 if PORT else 4
    L = [str(x)[:10] for x in sc.get("left_items", [])[:cap]]
    R = [str(x)[:10] for x in sc.get("right_items", [])[:cap]]
    n = len(L) + len(R)
    step = aw * 0.96 / (n + 1)
    bw = min(step * 0.8, 120 * K)
    total = n * step + step
    x = cx - total / 2 + (step - bw) / 2
    base = cy + ah * 0.40
    maxh = ah * 0.62
    shrink = prog(t, 0.28 * T, 0.6 * T)
    grow = prog(t, 0.45 * T, 0.85 * T)
    LH, RH = [1.0, 0.85, 0.7, 0.55], [0.5, 0.65, 0.58, 0.8]
    top = cy - ah / 2 + 20 * K
    lw = len(L) * step
    rw = len(R) * step
    lx0 = cx - total / 2
    rx0 = lx0 + lw + step
    ctext(d, lx0 + lw / 2, top, str(sc.get("left_title", "")).upper(), fit(d, str(sc.get("left_title", "")).upper(), "Bold", 36 * K, lw + step * 0.6), (255, 200, 90))
    ctext(d, rx0 + rw / 2, top, str(sc.get("right_title", "")).upper(), fit(d, str(sc.get("right_title", "")).upper(), "Bold", 36 * K, rw + step * 0.6), TEAL)
    for i, lab in enumerate(L):
        bx = lx0 + i * step + (step - bw) / 2
        hh = 10 + maxh * LH[i] * (1 - shrink)
        d.rounded_rectangle([bx, base - hh, bx + bw, base], radius=int(10 * K), fill=(255, 190, 70))
        ctext(d, bx + bw / 2, base + 28 * K, lab, fit(d, lab, "Medium", 22 * K, step * 0.95), (200, 215, 235))
    for i, lab in enumerate(R):
        bx = rx0 + i * step + (step - bw) / 2
        hh = 10 + maxh * RH[i] * grow
        d.rounded_rectangle([bx, base - hh, bx + bw, base], radius=int(10 * K), fill=TEAL)
        ctext(d, bx + bw / 2, base + 28 * K, lab, fit(d, lab, "Medium", 22 * K, step * 0.95), (200, 215, 235))
    d.line([(lx0 - 10, base + 2), (lx0 + total + 10, base + 2)], fill=(120, 150, 190), width=3)
    ar = prog(t, 0.3 * T, 0.4 * T)
    if ar > 0:
        ax, ay = lx0 + lw + step / 2, base - maxh * 0.5
        s = 22 * K * ar
        d.polygon([(ax - s, ay - s * 0.5), (ax, ay - s * 0.5), (ax, ay - s), (ax + s, ay), (ax, ay + s), (ax, ay + s * 0.5), (ax - s, ay + s * 0.5)], fill=(255, 255, 255))


def sc_chips(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    top = cy - ah / 2
    hy = top + 28 * K
    para(d, cx, hy, sc.get("heading", "").upper(), "Bold", 54 * K, aw, WHITE, maxlines=2)
    items = [str(x) for x in sc.get("items", [])[:5]]
    cols = 2 if PORT else 3
    cw, chh = aw / cols * 0.9, 96 * K
    pal = [(255, 150, 70), (90, 210, 200), (255, 205, 80), (150, 190, 255), (255, 120, 150)]
    rows = (len(items) + cols - 1) // cols
    y0 = top + 150 * K
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        in_row = min(cols, len(items) - r * cols)
        x = cx + (c - (in_row - 1) / 2) * (aw / cols)
        y = y0 + r * 135 * K
        st = 0.8 + i * (0.55 * T / max(1, len(items)))
        e = prog(t, st, st + 0.5)
        if e <= 0:
            continue
        yy = y + (1 - e) * 30 * K
        bgc = bgcol(yy)
        d.rounded_rectangle([x - cw / 2, yy - chh / 2, x + cw / 2, yy + chh / 2], radius=int(28 * K), fill=mix(bgc, pal[i % 5], e))
        ctext(d, x, yy, it.upper(), fit(d, it.upper(), "Bold", 38 * K, cw * 0.86), mix(bgc, (10, 20, 40), e))
    foot = sc.get("footer", "")
    if foot:
        fy = y0 + rows * 135 * K + 20 * K
        para(d, cx, fy, foot, "Medium", 32 * K, aw, mix(bgcol(fy), (190, 215, 240), prog(t, 0.6 * T, 0.75 * T)), maxlines=2)


def sc_timeline(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    hy = cy - ah / 2 + 28 * K
    para(d, cx, hy, sc.get("heading", "").upper(), "Bold", 54 * K, aw, WHITE, maxlines=2)
    ly = cy + ah * 0.08
    x0, x1 = cx - aw * 0.40, cx + aw * 0.26
    d.line([(x0, ly + 60 * K), (x1 + 60 * K, ly + 60 * K)], fill=(120, 160, 200), width=int(6 * K))
    for x in (x0, x1):
        d.ellipse([x - 12 * K, ly + 48 * K, x + 12 * K, ly + 72 * K], fill=GOLD)
    for x, key in ((x0, "left_label"), (x1, "right_label")):
        lab = str(sc.get(key, ""))
        lx = min(max(x, 40 + 130 * K), W - 40 - 130 * K)
        para(d, lx, ly + 115 * K, lab, "Medium", 28 * K, 260 * K, (200, 215, 235), lh=1.25, maxlines=3)
    tx = x1 + 100 * K
    for i, r in enumerate((52, 34, 16)):
        rr = r * K
        d.ellipse([tx - rr, ly + 60 * K - rr, tx + rr, ly + 60 * K + rr], fill=(235, 70, 70) if i % 2 == 0 else WHITE)
    p = prog(t, 0.15 * T, 0.8 * T)
    mx = lerp(x0, tx, p)
    my = ly + 24 * K - 130 * K * abs(math.sin(math.pi * 3 * p))
    rr = 30 * K
    d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=ORANGE, outline=WHITE, width=int(4 * K))


def sc_question(img, d, t, T, sc):
    cx, cy, aw, ah = ART
    p = prog(t, 0.2, 0.9)
    hy = cy - ah / 2 + 50 * K
    para(d, cx, hy, sc.get("heading", "").upper(), "Bold", 66 * K, aw, mix(bgcol(hy), ORANGE, p), lh=1.2, maxlines=3)
    qy = cy + ah * 0.30
    r = (72 + 6 * math.sin(t * 4)) * K
    ring(d, cx, qy, r, (200, 215, 235), 6 * K)
    ctext(d, cx, qy - 4 * K, "?", font("Bold", 100 * K), (200, 215, 235))


FN = {"title": sc_title, "globe": sc_globe, "gone": sc_gone, "bars": sc_bars, "chips": sc_chips, "timeline": sc_timeline, "question": sc_question}


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


def subtitle(d, sc, t, narr):
    if sc["type"] == "title":
        return
    ch = chunks(sc["narration"])
    total = sum(len(c) for c in ch) or 1
    tt = t - NARR_START
    if tt < -0.05 or tt > narr + 0.5:
        return
    acc, cur = 0, ch[-1]
    for c in ch:
        if tt <= (acc + len(c)) / total * narr + 0.02:
            cur = c
            break
        acc += len(c)
    f = font("Medium", SUB_FONT)
    lines = wrap(d, cur.strip().rstrip(",;:"), f, W * 0.88)
    lh = SUB_FONT * 1.38
    bh = lh * len(lines) + 28
    y0 = SUB_Y1 - bh
    wmax = max(d.textlength(l, font=f) for l in lines)
    d.rounded_rectangle([W / 2 - wmax / 2 - 30, y0, W / 2 + wmax / 2 + 30, SUB_Y1], radius=22, fill=(6, 10, 24))
    for i, l in enumerate(lines):
        ctext(d, W / 2, y0 + 14 + lh * i + lh / 2 - 2, l, f, WHITE)


def frame(sc, t, T, narr):
    img = _bg.copy()
    d = ImageDraw.Draw(img)
    FN.get(sc["type"], sc_gone)(img, d, t, T, sc)
    subtitle(d, sc, t, narr)
    f = min(1.0, t / 0.3, (T - t) / 0.3)
    if f < 1:
        img = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), img, max(0.0, f))
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


def tts_espeak(text, path):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe:
        with open(path, "wb") as f:
            subprocess.run([exe, "--stdout", "-s", "150", "-v", "en-us", text], stdout=f, check=True)
        return True
    import ctypes
    import espeakng_loader as e
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
    b = text.encode()
    lib.espeak_Synth(b, len(b) + 1, 0, 0, 0, 1, None, None)
    lib.espeak_Synchronize()
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(buf.tobytes())
    return True


def make_voice(text, idx):
    raw, norm = f"{WORK}/raw_{idx}.wav", f"{WORK}/n_{idx}.wav"
    if not tts_google(text, raw):
        print("Using offline voice for scene", idx)
        tts_espeak(text, raw)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", norm], check=True)
    with wave.open(norm) as w:
        return norm, w.getnframes() / w.getframerate()


# ---------------- build ----------------
def load(path):
    data = json.load(open(path))
    sc = data["scenes"] if isinstance(data, dict) else data
    for s in sc:
        s["narration"] = clean(s.get("narration", ""))
        s["type"] = s.get("type", "gone")
    return sc


def sample(scenes):
    os.makedirs(OUT, exist_ok=True)
    n = len(scenes)
    cols = 4 if PORT else 2
    th = (W // 2, H // 2) if PORT else (640, 360)
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
    for i, sc in enumerate(scenes):
        wav, n = make_voice(sc["narration"], i)
        T = NARR_START + n + (1.0 if sc["type"] == "title" else 0.7)
        T = math.ceil(T * FPS) / FPS
        info.append((sc, wav, n, T))
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
    with wave.open(WORK + "/full.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(allp.tobytes())
    final_tmp = OUT + "/video.part.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", WORK + "/list.txt", "-i", WORK + "/full.wav",
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", final_tmp], check=True)
    os.rename(final_tmp, OUT + "/video.mp4")
    print("done", OUT + "/video.mp4", round(sum(x[3] for x in info), 1), "s")


if __name__ == "__main__":
    mode, path = sys.argv[1], sys.argv[2]
    scenes = load(path)
    {"render": render, "sample": sample}[mode](scenes)
