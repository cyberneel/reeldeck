# Where Instagram's Reels UI sits over a cut: shades those zones on frames every
# few seconds and tiles them into one sheet, so text under the UI shows at a glance.
# usage: python3 safezone.py <cut.mp4> [sheet.png] [every_s]
import subprocess
import sys
from pathlib import Path

# On a 1080x1920 frame. ponytail: these are published numbers, not measured; calibrate
# them from a phone screenshot of a posted reel (the UI moves with the app and phone).
TOP = 250      # "Reels" title, camera, the handle once you scroll
BOTTOM = 440   # handle, two caption lines, audio row, the app's tab bar
RAIL = 150     # like / comment / share / save / audio, down the right side
RAIL_FROM = 1000
SIDE = 60      # rounded corners and phone widths


def zones() -> str:
    s = "ih/1920"
    box = lambda x, y, w, h: f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red@0.4:t=fill"
    return ",".join([
        box(0, 0, "iw", f"{TOP}*{s}"),
        box(0, f"ih-{BOTTOM}*{s}", "iw", f"{BOTTOM}*{s}"),
        box(f"iw-{RAIL}*{s}", f"{RAIL_FROM}*{s}", f"{RAIL}*{s}", f"ih-{RAIL_FROM}*{s}-{BOTTOM}*{s}"),
        box(0, f"{TOP}*{s}", f"{SIDE}*{s}", f"ih-{TOP + BOTTOM}*{s}"),
        box(f"iw-{SIDE}*{s}", f"{TOP}*{s}", f"{SIDE}*{s}", f"{RAIL_FROM - TOP}*{s}"),
    ])


def main(cut: str, out: str = "", every: str = "2.5") -> None:
    cut = Path(cut).resolve()
    out = Path(out or cut.with_name(cut.stem + "-safezone.png")).resolve()
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(cut)]))
    n = max(1, int(dur / float(every)))
    cols = min(n, 7)
    rows = -(-n // cols)
    vf = (f"fps=1/{every},scale=270:-2,{zones()},"
          f"drawtext=text='%{{pts\\:hms}}':x=6:y=6:fontsize=16:fontcolor=white:box=1:boxcolor=black@0.6,"
          f"tile={cols}x{rows}:padding=4:color=white")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cut), "-vf", vf, "-frames:v", "1", str(out)], check=True)
    print(out)


if __name__ == "__main__":
    main(*sys.argv[1:])
