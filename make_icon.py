#!/usr/bin/env python3
"""
Lucha Pinchadiscos アイコン生成
- ルチャドールマスクのみ（レコードなし）
- 下部に "LP" テキスト
"""
from PIL import Image, ImageDraw, ImageFont
import math, os, shutil, subprocess

def draw_icon(size):
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx  = size // 2
    cy  = int(size * 0.52)   # マスク中心（視覚的にセンター寄り）
    s   = size / 58.0        # スケール係数

    if size >= 32:
        # ── マスク本体（赤い楕円）────────────────────────────────
        mw, mh = int(20*s), int(22*s)
        draw.ellipse([cx-mw, cy-mh, cx+mw, cy+mh],
                     fill=(204, 17, 17), outline=(100, 0, 0), width=max(1, int(1.5*s)))

        # ── サイドパネル（青）────────────────────────────────────
        for sign in (-1, 1):
            pts = [
                (cx + sign*int(20*s), cy - int(10*s)),
                (cx + sign*int(8*s),  cy - int(18*s)),
                (cx + sign*int(8*s),  cy + int(20*s)),
                (cx + sign*int(20*s), cy + int(12*s)),
            ]
            draw.polygon(pts, fill=(17, 68, 204), outline=(8, 30, 120))

        # ── 額の三角（金）────────────────────────────────────────
        draw.polygon([
            (cx,             cy - int(22*s)),
            (cx - int(10*s), cy - int(8*s)),
            (cx + int(10*s), cy - int(8*s)),
        ], fill=(255, 204, 0), outline=(180, 120, 0))

    if size >= 64:
        # ── 額の星（アプリと同比率: 外r=6, 内r=3, 中心y=-14）────
        star_pts = []
        for i in range(10):
            a  = math.pi * 2 * i / 10 - math.pi / 2
            r2 = int(6*s) if i % 2 == 0 else int(3*s)
            star_pts.append((cx + r2*math.cos(a), cy - int(14*s) + r2*math.sin(a)))
        draw.polygon(star_pts, fill=(204, 17, 17))

    if size >= 32:
        # ── 目（金ひし形 + 白 + 黒瞳）アプリ準拠: 白=5s×4s, 瞳=2s×2s ─
        for sign in (-1, 1):
            ex = cx + sign * int(8*s)
            ey = cy - int(1*s)
            draw.polygon([
                (ex,            ey - int(5*s)),
                (ex - int(7*s), ey),
                (ex,            ey + int(5*s)),
                (ex + int(7*s), ey),
            ], fill=(255, 204, 0))
            draw.ellipse([ex-int(5*s), ey-int(4*s), ex+int(5*s), ey+int(4*s)],
                         fill=(238, 238, 238))
            draw.ellipse([ex-int(2*s), ey-int(2*s), ex+int(2*s), ey+int(2*s)],
                         fill=(17, 17, 17))

        # ── 鼻（金の逆三角）アプリ準拠: 幅10, y=12/4 ────────────
        if size >= 64:
            draw.polygon([
                (cx,            cy + int(12*s)),
                (cx - int(5*s), cy + int(4*s)),
                (cx + int(5*s), cy + int(4*s)),
            ], fill=(255, 204, 0))

        # ── 口元（金の横帯）アプリ準拠: 上辺y=14 ────────────────
        draw.polygon([
            (cx - int(12*s), cy + int(14*s)),
            (cx + int(12*s), cy + int(14*s)),
            (cx + int(10*s), cy + int(18*s)),
            (cx - int(10*s), cy + int(18*s)),
        ], fill=(255, 204, 0), outline=(180, 120, 0))

        # ── 放射状の装飾ライン ────────────────────────────────
        if size >= 128:
            for ang_d in [math.pi*0.25, math.pi*0.75, math.pi*1.25, math.pi*1.75]:
                x1 = cx + int(8*s) * math.cos(ang_d)
                y1 = cy + int(8*s) * math.sin(ang_d)
                x2 = cx + int(19*s) * math.cos(ang_d)
                y2 = cy + int(19*s) * math.sin(ang_d)
                draw.line([x1, y1, x2, y2], fill=(255, 204, 0), width=max(1, int(s*0.8)))

    return img


def main():
    iconset = "/Users/hiura/Desktop/LuchaPinchadiscos/LuchaPinchadiscos.iconset"
    os.makedirs(iconset, exist_ok=True)

    specs = [
        ("icon_16x16.png",       16),
        ("icon_16x16@2x.png",    32),
        ("icon_32x32.png",       32),
        ("icon_32x32@2x.png",    64),
        ("icon_128x128.png",    128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png",    256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png",    512),
        ("icon_512x512@2x.png",1024),
    ]
    for fname, sz in specs:
        img = draw_icon(sz)
        img.save(os.path.join(iconset, fname))
        print(f"  {fname} ({sz}px)")

    icns = "/Users/hiura/Desktop/LuchaPinchadiscos/LuchaPinchadiscos.icns"
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(iconset)
    print(f"\nIcon saved: {icns}")


if __name__ == "__main__":
    main()
