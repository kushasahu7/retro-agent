#!/usr/bin/env python3
"""Render captured terminal output to an SVG that GitHub can display inline.

Used to produce the README screenshots from the synthetic demo corpus, so no
real session data ever ends up in the repository.
"""
import html, os, subprocess, sys

# Plain family name: quoted CSS stacks are ignored by some SVG renderers,
# which then substitute a proportional font and overflow the canvas.
FONT = "monospace"
# Advance width used for BOTH textLength and canvas sizing. Renderers disagree
# on monospace metrics (browsers ~7.8px at 13px, some fall back near 11.7px),
# so pin every line to this width: correct renderers space slightly wider,
# wide-font renderers tighten, and neither clips.
CW, LH, FS = 9.0, 19.0, 13.0
# Canvas is sized for a renderer that ignores textLength and substitutes a
# wide font (measured ~11.7px advance). Browsers honour textLength and simply
# leave some right margin. Margin is cosmetic; clipping is not.
CW_CANVAS = 12.0
# Canvas sizing uses a wider advance than the nominal 0.6em: renderers
# substitute fonts and a too-narrow canvas clips the right-hand columns.
PAD_X, PAD_TOP, BAR = 18, 44, 30
BG, FG = "#0d1117", "#c9d1d9"
DIM, GREEN, YELLOW, RED, CYAN = "#6e7681", "#3fb950", "#d29922", "#f85149", "#58a6ff"

def colour(line):
    s = line.rstrip("\n")
    st = s.strip()
    if st.startswith("$ "): return None          # handled specially
    if st.startswith(("---", "===", "___")) or set(st) <= set("- ="): return DIM
    if "WARNING" in s or "do NOT" in s or "!!" in s: return YELLOW
    if "ZERO verification" in s or "FAIL" in s: return RED
    if st.startswith(("TOTALS", "HIGHEST-FRICTION", "SESSION", "SENSITIVE")): return CYAN
    if st.startswith("#") and set(st) <= set("# "): return CYAN
    return FG

def render(lines, title, out):
    width_chars = max([len(l.rstrip()) for l in lines] + [len(title) + 8, 60])
    w = int(width_chars * CW_CANVAS + PAD_X * 2)
    h = int(PAD_TOP + len(lines) * LH + 16)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="{FONT}" font-size="{FS}">',
         f'<rect width="{w}" height="{h}" rx="8" fill="{BG}"/>',
         f'<rect width="{w}" height="{BAR}" rx="8" fill="#161b22"/>',
         f'<rect y="{BAR-8}" width="{w}" height="8" fill="#161b22"/>']
    for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        o.append(f'<circle cx="{18+i*17}" cy="{BAR/2}" r="5.5" fill="{c}"/>')
    o.append(f'<text x="{w/2}" y="{BAR/2+4.5}" fill="{DIM}" text-anchor="middle" '
             f'font-size="11.5">{html.escape(title)}</text>')
    y = PAD_TOP + 4
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip():
            y += LH; continue
        if s.strip().startswith("$ "):
            ind = len(s) - len(s.lstrip())
            x = PAD_X + ind * CW
            body = s.strip()[2:]
            o.append(f'<text x="{x}" y="{y}" fill="{GREEN}" xml:space="preserve" '
                     f'textLength="{CW:.1f}" lengthAdjust="spacingAndGlyphs">$</text>')
            o.append(f'<text x="{x+CW*2}" y="{y}" fill="#ffffff" xml:space="preserve" '
                     f'textLength="{max(1.0,len(body)*CW):.1f}" '
                     f'lengthAdjust="spacingAndGlyphs">{html.escape(body)}</text>')
        else:
            tl = max(1.0, len(s) * CW)
            o.append(f'<text x="{PAD_X}" y="{y}" fill="{colour(s)}" xml:space="preserve" '
                     f'textLength="{tl:.1f}" lengthAdjust="spacingAndGlyphs">'
                     f'{html.escape(s)}</text>')
        y += LH
    o.append("</svg>")
    open(out, "w").write("\n".join(o))
    # Geometry assertion: every pinned line must fit inside the canvas. Browsers
    # honour textLength, so if this holds the SVG cannot clip in a browser.
    longest = max((len(l.rstrip()) for l in lines), default=0)
    need = PAD_X * 2 + longest * CW
    assert need <= w, f"{out}: content {need:.0f}px exceeds canvas {w}px"
    return w, h, longest, need

import re as _re

def scrub(lines, demo_root):
    """Screenshots go in a public repo: strip the sandbox path and the username."""
    user = os.path.basename(os.path.expanduser("~"))
    out = []
    for l in lines:
        l = l.replace(demo_root, "~/retro-agent")
        l = l.replace(demo_root.replace("/", "-"), "~/retro-agent")
        l = _re.sub(r"/Users/[A-Za-z0-9._-]+", "/Users/you", l)
        l = _re.sub(r"/home/[A-Za-z0-9._-]+/retro", "/home/you/retro", l)
        if user:
            l = _re.sub(rf"\b{_re.escape(user)}\b", "you", l)
        out.append(l)
    return out

def capture(cmd, env, cwd, max_lines=None, demo_root=""):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, cwd=cwd)
    out = (r.stdout + r.stderr).splitlines()
    if max_lines: out = out[:max_lines]
    return scrub(out, demo_root)

if __name__ == "__main__":
    demo = sys.argv[1]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs = os.path.join(repo, "docs")
    os.makedirs(docs, exist_ok=True)
    env = dict(os.environ,
               RETRO_PROJECTS=os.path.join(demo, "projects"),
               RETRO_CLAUDE=os.path.join(demo, "claude"),
               RETRO_ARCHIVE=os.path.join(demo, "arc"),
               RETRO_DB=os.path.join(demo, "demo.db"),
               RETRO_CODEX=os.path.join(demo, "codex", "sessions"),
               RETRO_CURSOR_DB=os.path.join(demo, "none.vscdb"))
    # Order matters: each command depends on the state the previous one left.
    subprocess.run("python3 retro.py consent --accept", shell=True, env=env,
                   cwd=repo, capture_output=True)
    subprocess.run("python3 retro.py archive", shell=True, env=env, cwd=repo,
                   capture_output=True)
    subprocess.run("python3 retro.py scan", shell=True, env=env, cwd=repo,
                   capture_output=True)
    shots = [
        ("friction", "retro friction", None),
        ("parity", "retro parity", None),
        ("sanitize", f"retro sanitize 'hero section' --out {os.path.join(demo,'bundle')}", None),
        ("consent", "retro consent", 22),
        ("heatmap-term", "retro heatmap --no-color", None),
        ("forget", "retro forget --all", None),
    ]
    for name, cmd, cap in shots:
        py = "python3 retro.py " + cmd.split(" ", 1)[1]
        shown = cmd if "--out" not in cmd else cmd.split(" --out")[0] + " --out ./bundle"
        lines = [f"$ {shown}"] + capture(py, env, repo, cap, demo)
        w, h, lc, need = render(lines, shown, os.path.join(docs, f"{name}.svg"))
        print(f"docs/{name}.svg  {w}x{h}  {len(lines)} lines  "
              f"widest {lc} chars -> {need:.0f}px fits in {w}px  OK")
    # the shareable SVG heatmap, drawn from the synthetic corpus
    subprocess.run(f"python3 retro.py heatmap --svg {os.path.join(docs,'heatmap.svg')} "
                   f"--no-color", shell=True, env=env, cwd=repo, capture_output=True)
    print("docs/heatmap.svg  (synthetic data)")

    # archive shot last, rebuilding from scratch so the numbers look like a first run
    lines = [f"$ retro archive"] + capture("python3 retro.py archive", env, repo, None, demo)
    w, h, lc, need = render(lines, "retro archive", os.path.join(docs, "archive.svg"))
    print(f"docs/archive.svg  {w}x{h}  {len(lines)} lines  "
          f"widest {lc} chars -> {need:.0f}px fits in {w}px  OK")
