import math, os, sys

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)

ORANGE = "#F08A50"
NAVY = "#1B2433"
CREAM = "#FFF4EA"
RUST = "#C2521C"
CX, CY = 128, 130
CHEV = (5, 115, 215)
FONT = 'font-family="JetBrains Mono, ui-monospace, monospace" font-weight="700"'


def pt(r, deg):
    a = math.radians(deg)
    return CX + r * math.cos(a), CY + r * math.sin(a)


def snake(fg, bg, r=78, hw=15, chevrons=(), eye=True, head_deg=-85, eye_fill=CREAM):
    s = hw / 15.0
    hs = s * 1.4
    parts = []
    # tapered tail: from the tip inside the mouth back to full width, eased so the join is smooth
    tip, full = head_deg + math.degrees(13 * s / r), head_deg + 55
    outer, inner_ = [], []
    n = 24
    for i in range(n + 1):
        u = i / n
        d = tip + (full - tip) * u
        w = hw * (0.42 + 0.58 * (1 - (1 - u) ** 2))
        outer.append(pt(r + w, d))
        inner_.append(pt(r - w, d))
    poly = outer + inner_[::-1]
    parts.append('<path fill="%s" d="M%s Z"/>' % (fg, " L".join("%.2f,%.2f" % p for p in poly)))
    # main body arc, from the tail's full width round to the neck
    a0, a1 = full - 1, head_deg + 360 - math.degrees(8 * s / r)
    p0, p1 = pt(r, a0), pt(r, a1)
    parts.append('<path fill="none" stroke="%s" stroke-width="%.1f" d="M%.2f,%.2f A%d,%d 0 1 1 %.2f,%.2f"/>'
                 % (fg, 2 * hw, p0[0], p0[1], r, r, p1[0], p1[1]))
    # chevrons pointing in the direction of travel (clockwise): plan, implement, review
    for d in chevrons:
        a, b, c = (-5 * s, -r - hw - 3), (5 * s, -r), (-5 * s, -r + hw + 3)
        parts.append('<polyline fill="none" stroke="%s" stroke-width="%.1f" stroke-linejoin="miter" points="%s" transform="translate(%d %d) rotate(%.1f)"/>'
                     % (bg, 4.5 * s, " ".join("%.1f,%.1f" % q for q in (a, b, c)), CX, CY, d + 90))
    # head, drawn in a local frame: x along the travel direction, top of the ring at (0, -r)
    Y = -r
    head = ("M-16,{n1} C-12,{sk1} 6,{sk2} 16,{sk3} C22,{sn1} 25,{sn2} 23,{sn3} L2,{mc} L19,{lj} C16,{ch1} 4,{ch2} -16,{n2} Z").format(
        n1=Y - 15, sk1=Y - 24, sk2=Y - 27, sk3=Y - 20, sn1=Y - 16, sn2=Y - 12, sn3=Y - 8,
        mc=Y - 1, lj=Y + 9, ch1=Y + 15, ch2=Y + 17, n2=Y + 15)
    front = ("M-4,{a} C6,{b} 12,{c} 16,{d} C22,{e} 25,{f} 23,{g} L2,{mc} L19,{lj} C16,{ch1} 8,{ch2} 0,{z}").format(
        a=Y - 26, b=Y - 27, c=Y - 24, d=Y - 20, e=Y - 16, f=Y - 12, g=Y - 8, mc=Y - 1, lj=Y + 9, ch1=Y + 15, ch2=Y + 17, z=Y + 17)
    tf = 'translate(%d %d) rotate(%.1f) translate(0 %.2f) scale(%.3f) translate(0 %.2f)' % (CX, CY, head_deg + 90, -r, hs, r)
    g = ['<g transform="%s">' % tf,
         '<path fill="none" stroke="%s" stroke-width="6" stroke-linejoin="round" d="%s"/>' % (bg, front),
         '<path fill="%s" d="%s"/>' % (fg, head)]
    if eye:
        g.append('<circle cx="3" cy="%d" r="5.2" fill="%s"/>' % (Y - 12, eye_fill))
        g.append('<circle cx="4.6" cy="%d" r="2.6" fill="%s"/>' % (Y - 12, fg))
    else:
        g.append('<circle cx="4" cy="%d" r="4.2" fill="%s"/>' % (Y - 12, bg))
    g.append('</g>')
    parts += g
    return "\n".join(parts)


def svg(body, bg=None, rx=56):
    rect = '<rect width="256" height="256" rx="%d" fill="%s"/>' % (rx, bg) if bg else ""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">%s\n%s</svg>\n'
            % (rect, body))


def inner(text):
    return text.split(">", 1)[1].rsplit("</svg>", 1)[0]


def cutout(fg, **kw):
    """The snake on a transparent background: notches, outline and eye are holes."""
    m = snake("#fff", "#000", eye_fill="#000", **kw)
    return ('<mask id="cut" maskUnits="userSpaceOnUse" x="0" y="0" width="256" height="256">%s</mask>'
            '<rect width="256" height="256" fill="%s" mask="url(#cut)"/>' % (m, fg))


def write(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


TILE = svg(snake(NAVY, ORANGE, chevrons=CHEV), ORANGE)

# Tiles
write("logo.svg", TILE)
write("logo-dark.svg", svg(snake(ORANGE, NAVY, chevrons=CHEV), NAVY))
write("apple-touch-icon.svg", svg(snake(NAVY, ORANGE, chevrons=CHEV), ORANGE, rx=0))
# Bare marks with real transparency, for light and dark backgrounds
write("mark.svg", svg(cutout(NAVY, chevrons=CHEV)))
write("mark-light.svg", svg(cutout(ORANGE, chevrons=CHEV)))
# Favicons: thicker body, fewer details
write("favicon.svg", svg(snake(NAVY, ORANGE, r=74, hw=24, chevrons=(115,), eye=False), ORANGE, rx=60))
write("favicon-16.svg", svg(snake(NAVY, ORANGE, r=72, hw=30, eye=False), ORANGE, rx=48))


def lockup(word_fg, word_accent):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 256" width="960" height="256">'
            '<g transform="translate(16 16) scale(0.875)">%s</g>'
            '<text x="276" y="166" %s font-size="124" letter-spacing="-3">'
            '<tspan fill="%s">spec</tspan><tspan fill="%s">loop</tspan></text></svg>\n'
            % (inner(TILE), FONT, word_fg, word_accent))


write("lockup.svg", lockup(NAVY, RUST))
write("lockup-dark.svg", lockup("#ECE9E2", ORANGE))

# Social preview card, 1200x630
og = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630">'
      '<rect width="1200" height="630" fill="#FBFAF7"/>'
      '<g transform="translate(96 150) scale(1.3)">%s</g>'
      '<text x="490" y="250" %s font-size="96" letter-spacing="-3"><tspan fill="%s">spec</tspan><tspan fill="%s">loop</tspan></text>'
      '<text x="494" y="340" font-family="Inter, Segoe UI, sans-serif" font-size="40" font-weight="600" fill="%s">Write the spec. Go make tea.</text>'
      '<text x="494" y="396" font-family="Inter, Segoe UI, sans-serif" font-size="40" font-weight="600" fill="%s">Come back to reviewed commits.</text>'
      '<text x="494" y="470" font-family="JetBrains Mono, Consolas, monospace" font-size="26" fill="#6B665D">plan → implement → review, for Claude Code</text>'
      '</svg>\n' % (inner(TILE), FONT, NAVY, RUST, NAVY, RUST))
write("og.svg", og)
print("ok")
