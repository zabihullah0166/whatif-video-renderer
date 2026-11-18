"""2.5D parallax: turns a still picture into moving footage by shifting near and far parts by different amounts.

A small depth model (Depth Anything V2 Small, runs on the CPU) estimates how far each pixel is.
Everything is optional: if opencv, transformers or the model download is missing, estimate() returns None
and render.py keeps its plain zoom and pan.

Environment:
  PARALLAX=0     switch off
  DEPTH_MODEL    Hugging Face model id (default depth-anything/Depth-Anything-V2-Small-hf), or "synthetic" for tests
"""
import os

import numpy as np

_pipe = {}


def available():
    if os.environ.get("PARALLAX", "1") == "0":
        return False
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def _get_pipe(model):
    if model not in _pipe:
        from transformers import pipeline
        _pipe[model] = pipeline("depth-estimation", model=model, device=-1)
    return _pipe[model]


def estimate(img):
    """PIL RGB image -> uint8 depth map of the same size (255 = near), or None if anything is missing."""
    if not available():
        return None
    try:
        import cv2
        w, h = img.size
        model = os.environ.get("DEPTH_MODEL") or "depth-anything/Depth-Anything-V2-Small-hf"
        if model == "synthetic":
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            d = 1.0 - np.sqrt(((xx / w - 0.5) * 1.3) ** 2 + (yy / h - 0.55) ** 2) * 1.6
            return (np.clip(d, 0, 1) * 255).astype(np.uint8)
        small = img.copy()
        small.thumbnail((768, 768))
        d = np.asarray(_get_pipe(model)(small)["depth"].convert("L"), dtype=np.float32)
        d = cv2.resize(d, (w, h), interpolation=cv2.INTER_CUBIC)
        lo, hi = np.percentile(d, 2), np.percentile(d, 98)
        d = np.clip((d - lo) / (hi - lo + 1e-6), 0, 1)
        d = cv2.GaussianBlur(d, (0, 0), max(w, h) / 300.0)
        return (d * 255).astype(np.uint8)
    except Exception as e:
        print("depth estimation unavailable:", repr(e)[:200], flush=True)
        return None


def warp(big, depth, W, H, wv, hv, cx, cy, sx, sy):
    """Crop the window (centre cx,cy, size wv x hv) of `big` to W x H, shifting each pixel by its depth.
    sx / sy are the shift in source pixels for the nearest pixel (the farthest moves the opposite way)."""
    import cv2
    xs = np.linspace(cx - wv / 2, cx + wv / 2, W, dtype=np.float32)
    ys = np.linspace(cy - hv / 2, cy + hv / 2, H, dtype=np.float32)
    mx = np.ascontiguousarray(np.broadcast_to(xs[None, :], (H, W)))
    my = np.ascontiguousarray(np.broadcast_to(ys[:, None], (H, W)))
    d = cv2.remap(depth, mx, my, cv2.INTER_LINEAR).astype(np.float32) / 255.0 - 0.5
    return cv2.remap(big, mx + d * sx, my + d * sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
