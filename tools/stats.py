"""Renders the profile charts in assets/ from live GitHub data (dark + light variants).

Runs daily in .github/workflows/stats.yml. Locally:  python tools/stats.py  (uses `gh auth token`)
Stdlib only.

Activity comes from the contribution calendar (private work counts there as anonymous numbers).
Languages count the lines I added in my own commits, not repository size, so vendored libraries
and other people's code don't skew them. Generated files (EF migrations, *.min.js, build output…)
are skipped.

Public repos are read through the API on every run. Private work repos can't be read by the
workflow token, so their totals are cached in data/private-languages.json (language -> lines only,
no repo names or code). Refresh that cache from local clones with:

    python tools/stats.py --private ~/path/to/work-repo [--private ~/another-repo]
"""

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import date, timedelta
from html import escape
from pathlib import Path

LOGIN = os.environ.get("GH_USER", "spahicnedim")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
PRIVATE_CACHE = ROOT / "data" / "private-languages.json"

ACCENT = "#4F8CFF"  # same accent as www.nedimspahic.com
THEMES = {
    "dark": dict(text="#e6edf3", muted="#8b949e", border="#30363d", track="#21262d", ok="#36D3A4",
                 palette=["#4F8CFF", "#36D3A4", "#A5C4FF", "#C3CAD6", "#7DE3C5", "#6e7681"]),
    "light": dict(text="#1f2328", muted="#59636e", border="#d0d7de", track="#eaeef2", ok="#1a9e78",
                  palette=["#4F8CFF", "#1a9e78", "#8FB3FF", "#57606a", "#5CCFAE", "#8c959f"]),
}
SANS = '-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'
MONO = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace'

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
  }
}"""


def token() -> str:
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def headers() -> dict:
    return {"Authorization": f"bearer {token()}", "User-Agent": "profile-stats", "Accept": "application/vnd.github+json"}


def fetch() -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"login": LOGIN}}).encode(),
        headers={**headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if "errors" in body:
        raise SystemExit(body["errors"])
    return body["data"]["user"]


def rest(path: str) -> list | dict:
    """GET from the REST API, following pagination for list endpoints."""
    url, out = f"https://api.github.com/{path}", None
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers()), timeout=30) as r:
            page = json.load(r)
            link = r.headers.get("Link") or ""
        if not isinstance(page, list):
            return page
        out = (out or []) + page
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return out or []


# ------------------------------------------------------------------ languages
EXT = {
    "cs": "C#", "razor": "Razor", "cshtml": "Razor",
    "ts": "TypeScript", "tsx": "TypeScript", "mts": "TypeScript",
    "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript",
    "py": "Python", "html": "HTML", "htm": "HTML", "css": "CSS", "scss": "SCSS",
    "cpp": "C++", "cc": "C++", "cxx": "C++", "hpp": "C++", "h": "C++", "c": "C",
    "java": "Java", "kt": "Kotlin", "swift": "Swift", "dart": "Dart", "go": "Go", "rs": "Rust",
    "php": "PHP", "sql": "SQL", "sh": "Shell", "bash": "Shell", "ps1": "PowerShell",
    "vue": "Vue", "svelte": "Svelte",
}
GENERATED = re.compile(
    r"(^|/)(node_modules|bin|obj|dist|build|out|\.next|coverage|vendor|wwwroot/lib|Migrations)/"
    r"|\.(min\.js|min\.css|g\.cs|Designer\.cs|d\.ts)$|ModelSnapshot\.cs$",
    re.IGNORECASE,
)
MAX_LINES_PER_FILE = 3000  # one file gaining more than this in one commit is pasted or generated code


def language(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if "." not in name or GENERATED.search(path):
        return None
    return EXT.get(name.rsplit(".", 1)[-1].lower())


def tally(changes) -> Counter:
    langs = Counter()
    for path, added in changes:
        lang = language(path)
        if lang and 0 < added <= MAX_LINES_PER_FILE:
            langs[lang] += added
    return langs


def public_lines() -> Counter:
    """Lines I added in my own (non-merge) commits to my public repositories."""
    changes = []
    for repo in rest(f"users/{LOGIN}/repos?type=owner&per_page=100"):
        if repo["fork"] or repo["private"]:
            continue
        try:
            commits = rest(f'repos/{repo["full_name"]}/commits?author={LOGIN}&per_page=100')
        except urllib.error.HTTPError:  # empty repository
            continue
        for c in commits:
            if len(c["parents"]) > 1:
                continue
            detail = rest(f'repos/{repo["full_name"]}/commits/{c["sha"]}')
            changes += [(f["filename"], f.get("additions", 0)) for f in detail.get("files", [])]
    return tally(changes)


def git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def private_lines(repos: list[Path]) -> Counter:
    """Lines I added on the default branch of local clones of private work repos."""
    total = Counter()
    for repo in repos:
        email = git(repo, "config", "user.email").strip()
        ref = "origin/HEAD" if subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "origin/HEAD"], capture_output=True
        ).returncode == 0 else "HEAD"
        changes = []
        for line in git(repo, "log", ref, "--no-merges", f"--author=<{re.escape(email)}>", "--numstat", "--format=").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit():
                changes.append((parts[2], int(parts[0])))
        total += tally(changes)
    return total


def load_private() -> Counter:
    if not PRIVATE_CACHE.exists():
        return Counter()
    return Counter(json.loads(PRIVATE_CACHE.read_text())["lines"])


def save_private(lines: Counter):
    PRIVATE_CACHE.parent.mkdir(exist_ok=True)
    PRIVATE_CACHE.write_text(json.dumps({
        "about": "Lines I added in private work repositories, per language. Only totals, no repo names or code. "
                 "Refreshed locally with: python tools/stats.py --private <path-to-clone>",
        "updated": date.today().isoformat(),
        "lines": dict(lines.most_common()),
    }, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ numbers
def summarize(user: dict, langs: Counter) -> dict:
    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [(date.fromisoformat(d["date"]), d["contributionCount"]) for w in cal["weeks"] for d in w["contributionDays"]]
    days.sort()
    today = days[-1][0]

    longest = run = 0
    for _, c in days:
        run = run + 1 if c else 0
        longest = max(longest, run)
    current = 0
    i = len(days) - 1
    if days[i][1] == 0:  # today not started yet doesn't break the streak
        i -= 1
    while i >= 0 and days[i][1]:
        current += 1
        i -= 1

    months = OrderedDict()
    y, m = today.year, today.month
    for _ in range(12):
        months[(y, m)] = 0
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months = OrderedDict(reversed(list(months.items())))
    for d, c in days:
        if (d.year, d.month) in months:
            months[(d.year, d.month)] += c

    # weekly totals for the sparkline; the current week is still running, so leave it out
    full = [w for w in cal["weeks"] if len(w["contributionDays"]) == 7]
    weeks = [sum(d["contributionCount"] for d in w["contributionDays"]) for w in full]

    total = sum(langs.values()) or 1
    top = [(k, v / total) for k, v in langs.most_common(5)]
    rest = 1 - sum(p for _, p in top)
    if rest > 0.0005:
        top.append(("Other", rest))

    return dict(
        total=cal["totalContributions"], longest=longest, current=current,
        active=sum(1 for _, c in days if c), months=months, weeks=weeks, langs=top, today=today,
    )


# ------------------------------------------------------------------ svg helpers
def svg(w, h, title, body, css=""):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="{escape(title)}"><title>{escape(title)}</title><style>{css}'
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}</style>"
        f"{body}</svg>"
    )


def frame(w, h, th):
    return f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="6" fill="none" stroke="{th["border"]}"/>'


def text(x, y, s, size, fill, family=SANS, weight=400, anchor="start", ls=0):
    return (
        f'<text x="{x}" y="{y}" font-family=\'{family}\' font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}" letter-spacing="{ls}">{escape(str(s))}</text>'
    )


def label(x, y, s, th, anchor="start"):
    return text(x, y, s.upper(), 11, th["muted"], MONO, 500, anchor, 0.8)


def fmt(n: int) -> str:
    return f"{n:,}"


def days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def nice_max(v: int, ticks: int = 4) -> int:
    """Smallest 'round' axis top (step of 1-8 x 10^k) that fits v in `ticks` steps."""
    mag = 1
    while True:
        for f in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8):
            step = f * mag
            if step * ticks >= v and step == int(step):
                return int(step * ticks)
        mag *= 10


GROW = (
    ".grow{animation:grow .9s cubic-bezier(.22,1,.36,1) both;transform-box:fill-box;transform-origin:50% 100%}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    ".fade{animation:fade .8s ease-out both}@keyframes fade{from{opacity:0}}"
)


# ------------------------------------------------------------------ cards
def stats_card(s, th):
    W, H = 440, 200
    b = [frame(W, H, th), label(24, 34, "Activity · last 12 months", th)]
    b.append(text(24, 88, fmt(s["total"]), 40, th["text"], SANS, 600, ls=-1))
    b.append(text(24, 112, "contributions", 14, th["muted"]))
    rows = [("Longest streak", days(s["longest"])), ("Current streak", days(s["current"])), ("Active days", fmt(s["active"]))]
    for i, (k, v) in enumerate(rows):
        y = 62 + i * 25
        b.append(text(236, y, k, 13, th["muted"]))
        b.append(text(W - 24, y, v, 13, th["text"], SANS, 600, "end"))
    # weekly sparkline
    wk = s["weeks"]
    x0, x1, y0, y1 = 24, W - 24, 136, 178
    mx = max(wk) or 1
    step = (x1 - x0) / (len(wk) - 1)
    pts = [(x0 + i * step, y1 - (v / mx) * (y1 - y0)) for i, v in enumerate(wk)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"M{x0},{y1} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{x1},{y1} Z"
    b.append(
        f'<defs><linearGradient id="sp" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{ACCENT}" stop-opacity=".35"/>'
        f'<stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></linearGradient></defs>'
    )
    b.append(f'<line x1="{x0}" y1="{y1 + .5}" x2="{x1}" y2="{y1 + .5}" stroke="{th["track"]}"/>')
    b.append(f'<g class="fade"><path d="{area}" fill="url(#sp)"/><polyline points="{line}" fill="none" stroke="{ACCENT}" stroke-width="1.6" stroke-linejoin="round"/></g>')
    lx, ly = pts[-1]
    b.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="{ACCENT}"/>')
    title = f'{fmt(s["total"])} contributions in the last 12 months; longest streak {s["longest"]} days; current streak {s["current"]} days; {s["active"]} active days'
    return svg(W, H, title, "".join(b), GROW)


def languages_card(s, th):
    W, H = 440, 200
    b = [frame(W, H, th), label(24, 34, "Languages · code I write", th)]
    x0, x1, y = 24, W - 24, 56
    b.append(f'<clipPath id="bar"><rect x="{x0}" y="{y}" width="{x1 - x0}" height="10" rx="5"/></clipPath><g clip-path="url(#bar)">')
    x = x0
    for i, (name, p) in enumerate(s["langs"]):
        w = (x1 - x0) * p
        color = th["palette"][-1] if name == "Other" else th["palette"][i]
        b.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 0):.1f}" height="10" fill="{color}"/>')
        x += w + (2 if w > 0 else 0)
    b.append("</g>")
    for i, (name, p) in enumerate(s["langs"]):
        col, row = i % 2, i // 2
        cx = 24 + col * 204
        cy = 100 + row * 32
        color = th["palette"][-1] if name == "Other" else th["palette"][i]
        b.append(f'<circle cx="{cx + 5}" cy="{cy - 4}" r="5" fill="{color}"/>')
        b.append(text(cx + 18, cy, name, 13, th["text"], SANS, 600))
        b.append(text(cx + 180, cy, f"{p * 100:.1f}%", 13, th["muted"], SANS, 400, "end"))
    title = "Share of lines I added in my own commits, public and private work: " + ", ".join(f"{n} {p * 100:.1f}%" for n, p in s["langs"])
    return svg(W, H, title, "".join(b), GROW)


def activity_chart(s, th):
    W, H = 896, 250
    b = [frame(W, H, th), label(24, 34, "Contributions per month", th)]
    b.append(text(W - 24, 34, f'{fmt(s["total"])} in the last year', 13, th["muted"], SANS, 400, "end"))
    months = list(s["months"].items())
    vals = [v for _, v in months]
    mx = max(vals) or 1
    nice = nice_max(max(mx, 4))
    x0, x1, y0, y1 = 64, W - 24, 64, 206
    for k in range(5):
        gy = y1 - (y1 - y0) * k / 4
        dash = "" if k == 0 else ' stroke-dasharray="3 4"'
        b.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="{th["track"]}"{dash}/>')
        b.append(text(x0 - 12, gy + 4, fmt(round(nice * k / 4)), 11, th["muted"], MONO, 400, "end"))
    slot = (x1 - x0) / len(months)
    bw = slot * 0.56
    peak = vals.index(mx)
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for i, ((yy, mm), v) in enumerate(months):
        bx = x0 + i * slot + (slot - bw) / 2
        bh = (y1 - y0) * v / nice
        op = 1 if i == peak else 0.55
        if v:
            b.append(
                f'<path class="grow" style="animation-delay:{i * 0.04:.2f}s" fill="{ACCENT}" fill-opacity="{op}" '
                f'd="M{bx:.1f},{y1} V{y1 - bh + 4:.1f} q0,-4 4,-4 H{bx + bw - 4:.1f} q4,0 4,4 V{y1} Z"/>'
            )
            b.append(text(bx + bw / 2, y1 - bh - 8, fmt(v), 11, th["text"] if i == peak else th["muted"], SANS, 600 if i == peak else 400, "middle"))
        lab = names[mm - 1] + (f" '{str(yy)[2:]}" if mm == 1 or i == 0 else "")
        b.append(text(bx + bw / 2, y1 + 22, lab, 11, th["muted"], MONO, 400, "middle"))
    title = "Contributions per month: " + ", ".join(f"{names[m - 1]} {y}: {v}" for (y, m), v in months)
    return svg(W, H, title, "".join(b), GROW)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private", action="append", type=Path, default=[], metavar="PATH",
                    help="local clone of a private work repo; refreshes data/private-languages.json")
    args = ap.parse_args()
    if args.private:
        save_private(private_lines([p.expanduser() for p in args.private]))
    private = load_private()
    public = public_lines()
    print("public lines:", dict(public.most_common(6)))
    print("private lines:", dict(private.most_common(6)))
    s = summarize(fetch(), public + private)
    OUT.mkdir(exist_ok=True)
    for name, th in THEMES.items():
        (OUT / f"stats-{name}.svg").write_text(stats_card(s, th), encoding="utf-8")
        (OUT / f"languages-{name}.svg").write_text(languages_card(s, th), encoding="utf-8")
        (OUT / f"activity-{name}.svg").write_text(activity_chart(s, th), encoding="utf-8")
    print(f'{s["total"]} contributions · longest {s["longest"]} · current {s["current"]} · active {s["active"]}')
    print("langs:", ", ".join(f"{n} {p:.1%}" for n, p in s["langs"]))


if __name__ == "__main__":
    main()
