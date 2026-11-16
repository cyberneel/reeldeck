#!/usr/bin/env python3
"""Keep only the largest blob in a matte, and nothing else.

A matte pulled from footage almost always comes with litter: specks of
background that survived the key and then show up in the composite as bright
dots where there should be nothing. Eroding them away also eats the real edge
and any thin detail (a watch crown, a cable), so the fix is not morphological —
it is topological. Label the connected regions, keep the biggest, drop the rest,
and multiply the result back over the original so the feathered edge survives
untouched.

    python3 cleanmask.py mask.png [out.png]
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


def clean(src: Path, dst: Path, thresh: int = 128) -> dict:
    img = Image.open(src).convert("L")
    a = np.array(img)
    solid = a > thresh
    labels, n = ndimage.label(solid)
    if n == 0:
        raise SystemExit(f"{src}: nothing above the threshold — is this a matte?")
    # component 0 is the background, so count from 1
    sizes = ndimage.sum(solid, labels, range(1, n + 1))
    keep = int(np.argmax(sizes)) + 1
    mask = labels == keep
    out = np.where(mask, a, 0).astype(np.uint8)
    Image.fromarray(out).save(dst)
    return {"components": n, "kept_px": int(sizes[keep - 1]),
            "dropped": int(sizes.sum() - sizes[keep - 1]), "dropped_blobs": n - 1}


def demo() -> None:
    """A big square plus two specks must come back as just the square."""
    import tempfile
    a = np.zeros((60, 60), np.uint8)
    a[10:40, 10:40] = 255      # the subject
    a[50, 5] = 255             # litter
    a[52:54, 20:22] = 255      # more litter
    with tempfile.TemporaryDirectory() as d:
        s, o = Path(d) / "m.png", Path(d) / "o.png"
        Image.fromarray(a).save(s)
        r = clean(s, o)
        got = np.array(Image.open(o))
        assert r["dropped_blobs"] == 2, r
        assert got[10:40, 10:40].min() == 255, "ate the subject"
        assert got[50, 5] == 0 and got[52, 20] == 0, "left litter behind"
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        demo()
    else:
        src = Path(sys.argv[1])
        dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
        print(" ", clean(src, dst))
