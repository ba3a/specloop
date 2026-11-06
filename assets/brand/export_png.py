"""Rasterize the SVGs with headless Chrome, downscale with ffmpeg and build favicon.ico.

Needs Chrome and ffmpeg; set CHROME and FFMPEG if they are not in the default places.
"""
import os, shutil, struct, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = os.path.join(HERE, "png")
CHROME = os.environ.get("CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
TMP = tempfile.mkdtemp()


def render(svg, w, h, scale=4):
    """Screenshot an SVG at scale x its size, with a transparent background."""
    out = os.path.join(TMP, "%s@%d.png" % (svg, scale))
    url = "file:///" + os.path.join(HERE, svg).replace("\\", "/")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--force-device-scale-factor=%d" % scale,
                    "--window-size=%d,%d" % (w, h), "--screenshot=" + out, url],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def scale(src, dest, w, h):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src,
                    "-vf", "scale=%d:%d:flags=lanczos,format=rgba" % (w, h), dest], check=True)
    return dest


os.makedirs(PNG, exist_ok=True)
logo = render("logo.svg", 256, 256)
fav = render("favicon.svg", 256, 256)
fav16 = render("favicon-16.svg", 256, 256)
jobs = [
    (logo, "logo-512.png", 512, 512),
    (render("logo-dark.svg", 256, 256), "logo-dark-512.png", 512, 512),
    (render("mark.svg", 256, 256), "mark-512.png", 512, 512),
    (render("mark-light.svg", 256, 256), "mark-light-512.png", 512, 512),
    (render("apple-touch-icon.svg", 256, 256), "apple-touch-icon.png", 180, 180),
    (logo, "icon-192.png", 192, 192),
    (logo, "icon-512.png", 512, 512),
    (fav16, "favicon-16.png", 16, 16),
    (fav, "favicon-32.png", 32, 32),
    (render("lockup.svg", 960, 256, 2), "lockup.png", 960, 256),
    (render("lockup-dark.svg", 960, 256, 2), "lockup-dark.png", 960, 256),
    (render("og.svg", 1200, 630, 2), "og.png", 1200, 630),
]
for src, name, w, h in jobs:
    scale(src, os.path.join(PNG, name), w, h)

# favicon.ico with PNG entries: 16 from the 16 px drawing, 32 and 48 from favicon.svg
entries = []
for src, px in ((fav16, 16), (fav, 32), (fav, 48)):
    with open(scale(src, os.path.join(TMP, "ico-%d.png" % px), px, px), "rb") as f:
        entries.append((px, f.read()))
data = struct.pack("<HHH", 0, 1, len(entries))
offset = 6 + 16 * len(entries)
for px, png in entries:
    data += struct.pack("<BBBBHHII", px, px, 0, 0, 1, 32, len(png), offset)
    offset += len(png)
data += b"".join(png for _, png in entries)
with open(os.path.join(HERE, "favicon.ico"), "wb") as f:
    f.write(data)
print("ok")
