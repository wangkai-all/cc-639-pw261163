"""Pixel preview rendering (PNG). Preview exists only to help operators
verify that the right instance was received — it is not a diagnostic view.
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image
from pydicom.dataset import Dataset


def has_pixels(ds: Dataset) -> bool:
    return "PixelData" in ds


def render_png(ds: Dataset) -> bytes:
    """Render first frame of a monochrome instance to PNG bytes."""
    arr = ds.pixel_array  # pydicom applies RescaleSlope/Intercept? No — raw.
    arr = arr.astype(np.float64)
    if arr.ndim == 3:  # multi-frame: first frame only
        arr = arr[0]

    slope = float(getattr(ds, "RescaleSlope", 1) or 1)
    intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = arr * slope + intercept

    wc = getattr(ds, "WindowCenter", None)
    ww = getattr(ds, "WindowWidth", None)
    try:
        center = float(wc[0] if isinstance(wc, (list, tuple)) else wc)
        width = float(ww[0] if isinstance(ww, (list, tuple)) else ww)
        if width <= 0:
            raise ValueError
    except (TypeError, ValueError):
        center = float(arr.min() + arr.max()) / 2.0
        width = float(arr.max() - arr.min()) or 1.0

    lo, hi = center - width / 2.0, center + width / 2.0
    arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    img = Image.fromarray((arr * 255.0).astype(np.uint8), mode="L")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
