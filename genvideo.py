"""Free generative video clips from public Hugging Face Spaces (ZeroGPU), called with gradio_client.

Nothing here is required: render.py imports this lazily and falls back to stock footage or animated stills
whenever a Space is down, slow, or out of free quota.

Environment:
  HF_TOKEN          free Hugging Face token (read). Without it the much smaller anonymous quota is used.
  HF_VIDEO_SPACES   comma separated Spaces to try in order (default: zerogpu-aoti/wan2-2-fp8da-aoti-faster)
  HF_VIDEO_API      endpoint name, only if auto detection picks the wrong one (e.g. /generate_video)
  GEN_SECONDS       length of each generated clip, default 3.5
  GEN_CLIP_TIMEOUT  give up on one clip after this many seconds, default 150
  GEN_VIDEO=0       switch generation off
"""
import os, random, re, shutil, subprocess, time

DEFAULT_SPACE = "zerogpu-aoti/wan2-2-fp8da-aoti-faster"
_state = {"disabled": False, "fails": 0, "reason": ""}


def spaces():
    raw = os.environ.get("HF_VIDEO_SPACES") or DEFAULT_SPACE
    return [x.strip() for x in raw.split(",") if x.strip()]


def enabled():
    return os.environ.get("GEN_VIDEO", "1") != "0" and not _state["disabled"] and bool(spaces())


def _client(space, token):
    from gradio_client import Client
    if token:
        for key in ("token", "hf_token"):
            try:
                return Client(space, verbose=False, **{key: token})
            except TypeError:
                continue
    return Client(space, verbose=False)


def _pick_endpoint(client):
    info = client.view_api(print_info=False, return_format="dict") or {}
    eps = info.get("named_endpoints") or {}
    want = os.environ.get("HF_VIDEO_API", "").strip()
    if want:
        if want not in eps:
            raise RuntimeError(f"endpoint {want} not found, available: {list(eps)}")
        return want, eps[want]
    best = None
    for name, ep in eps.items():
        names = " ".join(str(p.get("parameter_name") or p.get("label") or "") for p in ep.get("parameters", [])).lower()
        score = 2 * ("video" in name.lower()) + ("generate" in name.lower()) + ("prompt" in names) + ("image" in names)
        if best is None or score > best[0]:
            best = (score, name, ep)
    if best is None:
        raise RuntimeError("Space exposes no named API endpoints")
    return best[1], best[2]


def _kwargs(ep, prompt, image, seconds):
    """Fill only what we know (prompt, image, duration, seed); everything else keeps the Space's own default."""
    from gradio_client import handle_file
    kw = {}
    for p in ep.get("parameters", []):
        name = p.get("parameter_name")
        low = str(name or "").lower()
        comp = str(p.get("component") or "").lower()
        has_default = bool(p.get("parameter_has_default"))
        if comp in ("image", "file", "video", "imageeditor") or "image" in low:
            if image:
                kw[name] = handle_file(image)
            elif not has_default:
                raise RuntimeError(f"{name} needs an input image")
        elif "negative" in low:
            continue
        elif "prompt" in low or low in ("text", "caption"):
            kw[name] = prompt
        elif "duration" in low or low in ("seconds", "length"):
            kw[name] = seconds
        elif low == "seed":
            kw[name] = random.randint(0, 2 ** 31 - 1)
        elif "randomize" in low:
            kw[name] = True
        elif not has_default:
            raise RuntimeError(f"unknown required parameter {name}")
    return kw


def _find_video(x):
    if isinstance(x, str):
        return x if re.search(r"\.(mp4|webm|mov)$", x, re.I) and os.path.exists(x) else None
    if isinstance(x, dict):
        for k in ("video", "path", "name", "value"):
            r = _find_video(x.get(k))
            if r:
                return r
        x = list(x.values())
    if isinstance(x, (list, tuple)):
        for v in x:
            r = _find_video(v)
            if r:
                return r
    return None


def _note_failure(e):
    msg = str(e).lower()
    if "quota" in msg or "runs limit" in msg or "try again in" in msg:
        _state.update(disabled=True, reason="free GPU quota used up")
    else:
        _state["fails"] += 1
        if _state["fails"] >= 2:
            _state.update(disabled=True, reason="two failures in a row")


def generate(space, image, prompt, seconds, out, timeout):
    c = _client(space, os.environ.get("HF_TOKEN", "").strip() or None)
    name, ep = _pick_endpoint(c)
    job = c.submit(api_name=name, **_kwargs(ep, prompt, image, seconds))
    try:
        res = job.result(timeout=timeout)
    except BaseException:
        try:
            job.cancel()
        except Exception:
            pass
        raise
    path = _find_video(res)
    if not path:
        raise RuntimeError("Space returned no video file")
    shutil.copy(path, out)
    return True


def prepare(src, dst):
    """Make a short model clip usable as a long background: smooth 30 fps, no audio, and a seamless
    forward-and-back loop so it can repeat for the whole scene without a visible jump."""
    fc = "[0:v]framerate=fps=30[v];[v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[o]"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-an", "-filter_complex", fc, "-map", "[o]",
                        "-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-pix_fmt", "yuv420p", dst],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and os.path.exists(dst)


def clip_for_scene(i, image, prompt, workdir):
    """Returns the path of a prepared generated clip, or None. Never raises."""
    if not enabled():
        return None
    seconds = float(os.environ.get("GEN_SECONDS") or 3.5)
    timeout = float(os.environ.get("GEN_CLIP_TIMEOUT") or 150)
    for space in spaces():
        raw, done = f"{workdir}/gen_raw_{i}.mp4", f"{workdir}/gen_{i}.mp4"
        t0 = time.time()
        try:
            generate(space, image, prompt, seconds, raw, timeout)
            if prepare(raw, done):
                _state["fails"] = 0
                print(f"scene {i}: generated clip from {space} in {time.time() - t0:.0f}s", flush=True)
                return done
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            print(f"scene {i}: {space} failed after {time.time() - t0:.0f}s: {str(e)[:200]}", flush=True)
            _note_failure(e)
            if _state["disabled"]:
                print("generation switched off for this run:", _state["reason"], flush=True)
                return None
    return None
