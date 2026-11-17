"""Draw the PianoTalk icon (a speech bubble holding piano keys) with AppKit.

Writes:
  assets/icon-1024.png      master image
  mcpb/icon.png             512 px, for the Claude desktop extension
  app/PianoTalk.icns        for PianoTalk Recorder.app (via iconutil)

Run: .venv/bin/python scripts/make-icon.py
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSGradient,
    NSGraphicsContext,
    NSMakeRect,
    NSPNGFileType,
    NSShadow,
)
from Foundation import NSMakePoint, NSMakeSize

ROOT = Path(__file__).resolve().parent.parent
S = 1024  # canvas; macOS icon grid: 824 px rounded square centered, radius ~185


def rgb(hexstr: str, a: float = 1.0) -> NSColor:
    r, g, b = (int(hexstr[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return NSColor.colorWithDeviceRed_green_blue_alpha_(r, g, b, a)


def rounded(x, y, w, h, r):
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), r, r)


def draw() -> None:
    # --- background tile with a soft drop shadow -------------------------------
    tile_x, tile_y, tile = 100, 100, 824
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(rgb("000000", 0.35))
    shadow.setShadowOffset_(NSMakeSize(0, -12))
    shadow.setShadowBlurRadius_(28)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    bg = rounded(tile_x, tile_y, tile, tile, 185)
    rgb("2B2F77").setFill()
    bg.fill()
    NSGraphicsContext.restoreGraphicsState()
    grad = NSGradient.alloc().initWithStartingColor_endingColor_(rgb("5B6CF0"), rgb("2A2D7C"))
    grad.drawInBezierPath_angle_(bg, -90)

    # --- speech bubble ------------------------------------------------------------
    bx, by, bw, bh, br = 214, 330, 596, 420, 92
    bubble = rounded(bx, by, bw, bh, br)
    tail = NSBezierPath.bezierPath()
    tail.moveToPoint_(NSMakePoint(bx + 150, by + 30))
    tail.lineToPoint_(NSMakePoint(bx + 112, by - 120))
    tail.lineToPoint_(NSMakePoint(bx + 290, by + 30))
    tail.closePath()
    bubble.appendBezierPath_(tail)

    NSGraphicsContext.saveGraphicsState()
    s2 = NSShadow.alloc().init()
    s2.setShadowColor_(rgb("10123F", 0.45))
    s2.setShadowOffset_(NSMakeSize(0, -10))
    s2.setShadowBlurRadius_(24)
    s2.set()
    rgb("FFFFFF").setFill()
    bubble.fill()
    NSGraphicsContext.restoreGraphicsState()

    # --- keyboard inside the bubble (clipped to the bubble body) --------------------
    kx, ky, kw, kh = bx + 56, by + 56, bw - 112, bh - 112
    NSGraphicsContext.saveGraphicsState()
    rounded(kx, ky, kw, kh, 36).addClip()
    rgb("F3F4FB").setFill()
    NSBezierPath.fillRect_(NSMakeRect(kx, ky, kw, kh))
    n_white = 7
    ww = kw / n_white
    rgb("C9CCE6").setFill()
    for i in range(1, n_white):  # gaps between white keys
        NSBezierPath.fillRect_(NSMakeRect(kx + i * ww - 4, ky, 8, kh))
    black_h = kh * 0.58
    black_w = ww * 0.58
    for i in (1, 2, 4, 5, 6):  # C D | F G A pattern starting on C: blacks after keys 1,2,4,5,6
        x = kx + i * ww - black_w / 2
        rgb("23264F").setFill()
        rounded(x, ky + kh - black_h, black_w, black_h + 20, 14).fill()
    NSGraphicsContext.restoreGraphicsState()


def render(path: Path, size: int) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_(NSMakeSize(size, size))
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setImageInterpolation_(3)
    ctx.setCompositingOperation_(NSCompositingOperationSourceOver)
    from AppKit import NSAffineTransform
    t = NSAffineTransform.transform()
    t.scaleBy_(size / S)
    t.concat()
    draw()
    ctx.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    path.parent.mkdir(parents=True, exist_ok=True)
    rep.representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(str(path), True)


def main() -> None:
    render(ROOT / "assets" / "icon-1024.png", 1024)
    render(ROOT / "mcpb" / "icon.png", 512)
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "PianoTalk.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            render(iconset / f"icon_{base}x{base}.png", base)
            render(iconset / f"icon_{base}x{base}@2x.png", base * 2)
        out = ROOT / "app" / "PianoTalk.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print("wrote assets/icon-1024.png, mcpb/icon.png, app/PianoTalk.icns")


if __name__ == "__main__":
    main()
