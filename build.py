#!/usr/bin/env python3
"""Static-site generator for the Ley Lab website.

Assembles src/pages/*.html into build/*.html using src/partials/*.html plus
src/site.json and src/data/*, copies assets, generates one page per team member,
and writes a search index. Python 3 stdlib only; runs offline.

    python3 build.py            # build into build/
    python3 build.py --serve    # build, then serve on :8000

Publications come from src/data/publications.json (python3 update_publications.py)
and citation counts from src/data/citations.json (python3 fetch_citations.py).
The team roster is src/data/team.txt.
"""
import json
import re
import shutil
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "build"
TOKEN = re.compile(r"\{\{\s*(include:)?([a-z_]+)\s*\}\}")
FRONT = re.compile(r"^\s*<!--\s*(.*?)\s*-->\s*\n?", re.S)


def die(msg):
    print(f"build.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- rendering ---
def render(text, ctx, depth=0):
    if depth > 12:
        die("include recursion too deep")

    def sub(m):
        is_include, name = m.group(1), m.group(2)
        if is_include:
            frag = ctx["_inc"].get(name)
            if frag is None:
                p = SRC / "partials" / f"{name}.html"
                if not p.exists():
                    die(f"missing include: {name}")
                frag = p.read_text()
            return render(frag, ctx, depth + 1)
        if name not in ctx:
            die(f"unknown variable {{{{ {name} }}}} (page {ctx.get('page_file', '?')})")
        return str(ctx[name])

    return TOKEN.sub(sub, text)


# ------------------------------------------------------------ computed bits ---
def nav_links(site, active):
    out = []
    for item in site["nav"]:
        cls = "nav-link active" if item["id"] == active else "nav-link"
        out.append(
            f'<li class="nav-item"><a class="{cls}" href="{item["file"]}">{item["label"]}</a></li>'
        )
    return "\n".join(out)


def profile_links(site):
    out = []
    for s in site["socials"]:
        if s.get("disabled") or not s.get("href"):
            out.append(
                f'<!-- {s["label"]}: add URL in site.json to show this icon -->'
            )
            continue
        href = s["href"].replace("&", "&amp;")
        if s.get("mask"):
            inner = f'<span class="icon-mask icon-mask-{s["mask"]}"></span>'
        else:
            inner = f'<i class="{s["icon"]}"></i>'
        ext = "" if href.startswith("mailto:") else ' target="_blank" rel="noopener"'
        out.append(
            f'<a href="{href}" class="icon-link" title="{s["label"]}"'
            f' aria-label="{s["label"]}"{ext}>{inner}</a>'
        )
    return "\n".join(out)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_team(path):
    """Parse src/data/team.txt -> (members, alumni).

    members: list of dicts {name, title, photo, match, email, scholar, github,
             website, slug, is_pi}
    alumni:  list of dicts {name, years, position}
    """
    members, alumni, section = [], [], "members"
    if not path.exists():
        return members, alumni
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip() if raw.strip().startswith("#") else raw.strip()
        if not line:
            continue
        if line.upper().startswith("=== ALUMNI"):
            section = "alumni"
            continue
        parts = [p.strip() for p in line.split("|")]
        if section == "members":
            parts += [""] * (8 - len(parts))
            name, title, photo, match, email, scholar, github, website = parts[:8]
            if not name:
                continue
            members.append({
                "name": name, "title": title, "photo": photo or "photo.jpg",
                "match": match, "email": email, "scholar": scholar,
                "github": github, "website": website,
                "slug": slugify(name), "is_pi": not members,
            })
        else:
            parts += [""] * (4 - len(parts))
            name, years, position, link = parts[:4]
            if name:
                alumni.append({"name": name, "years": years,
                               "position": position, "link": link})
    return members, alumni


def _icon_link(href, title, icon, mailto=False):
    if not href:
        return ""
    if not mailto and not href.startswith(("http://", "https://", "mailto:")):
        return ""
    ext = "" if href.startswith("mailto:") else ' target="_blank" rel="noopener"'
    return (f'<a href="{href}" class="icon-link" title="{title}" aria-label="{title}"{ext}>'
            f'<i class="{icon}"></i></a>')


def member_links_html(m):
    out = [
        _icon_link(f'mailto:{m["email"]}' if m["email"] else "", "Email", "fa-solid fa-envelope", mailto=True),
        _icon_link(m["scholar"], "Google Scholar", "ai ai-google-scholar"),
        _icon_link(f'https://github.com/{m["github"]}' if m["github"] and "/" not in m["github"]
                   else m["github"], "GitHub", "fa-brands fa-github"),
        _icon_link(m["website"], "Website", "fa-solid fa-house"),
    ]
    return "\n".join(x for x in out if x)


def team_grid(members):
    if not members:
        return '<p>Team roster coming soon.</p>'
    pi = members[0]
    cards = []
    for m in members[1:]:
        photo = f'<img src="assets/img/{m["photo"]}" class="team-photo" alt="{m["name"]}" loading="lazy">'
        head = (f'{photo}<h4 class="team-name">{m["name"]}</h4>'
                f'<p class="team-info">{m["title"]}</p>')
        if m["match"]:
            # whole card links to the member page; no nested <a> icon links here
            cards.append(f'<a class="team-card" href="member-{m["slug"]}.html">{head}</a>')
        else:
            links = member_links_html(m)
            links_html = f'<div class="team-links">{links}</div>' if links else ""
            cards.append(f'<div class="team-card">{head}{links_html}</div>')
    pi_photo = f'<img src="assets/img/{pi["photo"]}" class="pi-photo" alt="{pi["name"]}" loading="lazy">'
    pi_name = (f'<a href="member-{pi["slug"]}.html" class="pi-name-link">{pi["name"]}</a>'
               if pi["match"] else pi["name"])
    pi_card = (
        '<div class="section-card">\n<div class="pi-card">\n'
        f'{pi_photo}\n<div>\n<h3 class="pi-name">{pi_name}</h3>\n'
        f'<p style="font-style: italic; color: var(--text-secondary);">{pi["title"]}</p>\n'
        f'<div class="pi-links">{member_links_html(pi)}</div>\n</div>\n</div>\n</div>'
    )
    return f'{pi_card}\n<div class="team-grid">\n' + "\n".join(cards) + '\n</div>'


def alumni_box(alumni):
    if not alumni:
        return ""
    def name_cell(a):
        if a.get("link"):
            return f'<a href="{a["link"]}" target="_blank" rel="noopener">{a["name"]}</a>'
        return a["name"]
    rows = "\n".join(
        f'<tr><td>{name_cell(a)}</td><td>{a["years"]}</td><td>{a["position"]}</td></tr>'
        for a in alumni
    )
    return (
        '<div class="section-card">\n<h3 style="margin-top: 0;">Lab Alumni</h3>\n'
        '<table class="alumni-table">\n'
        '<thead><tr><th>Name</th><th>Years</th><th>Current position</th></tr></thead>\n'
        f'<tbody>\n{rows}\n</tbody>\n</table>\n</div>'
    )


def cite_key(e):
    return e["doi"].lower() if e.get("doi") else (f'pmid:{e["pmid"]}' if e.get("pmid") else "")


def _pill(cls, href, label):
    return f'<a class="btn-pill {cls}" href="{href}" target="_blank" rel="noopener">{label}</a>'


KIND_TAG = {"abstract": "Meeting abstract", "other": "Erratum / comment"}


def render_pub(e, cites=None):
    kind = e.get("kind", "publication")
    tag = f'<span class="pub-kind-tag">{KIND_TAG[kind]}</span> ' if kind in KIND_TAG else ""
    parts = []
    if e.get("authors_html"):
        parts.append(e["authors_html"])
    if e.get("journal"):
        parts.append(f'<span class="pub-venue">{e["journal"]}</span>')
    if e.get("year"):
        parts.append(str(e["year"]))
    meta = tag + ". ".join(parts) + ("." if parts else "")

    actions = []
    if e.get("doi"):
        actions.append(_pill("btn-doi", f'https://doi.org/{e["doi"]}', "DOI"))
    elif e.get("link_kind") == "arxiv":
        actions.append(_pill("btn-arxiv", e["url"], "arXiv"))
    if e.get("pmid"):
        actions.append(_pill("btn-pubmed", f'https://pubmed.ncbi.nlm.nih.gov/{e["pmid"]}/', "PubMed"))
    if not actions:
        actions.append(_pill("btn-doi", e["url"], "Link"))

    hit = (cites or {}).get(cite_key(e)) if cites else None
    n_cites = hit["cited_by_count"] if hit and hit.get("cited_by_count") else 0
    if n_cites:
        href = f'https://openalex.org/works/{hit["openalex_id"]}'
        actions.append(f'<a class="pub-cites" href="{href}" target="_blank" '
                       f'rel="noopener">Cited by {n_cites:,}</a>')

    return (
        f'<div class="pub-entry fade-in-section" data-pub-searchable '
        f'data-kind="{e.get("kind", "publication")}" '
        f'data-year="{e["year"]}" data-n="{e["n"]}" data-cites="{n_cites}">\n'
        f'<a class="pub-title" href="{e["url"]}">{e["title"]}</a>\n'
        f'<div class="pub-meta">{meta}</div>\n'
        f'<div class="pub-actions">\n' + "\n".join(actions) + '\n</div>\n</div>'
    )


def recent_publications(pubs, limit=4):
    """Short references (no author list) for the N most recent papers."""
    entries = [e for e in pubs["entries"] if e.get("kind", "publication") == "publication"][:limit]
    if not entries:
        return '<p class="pub-empty">No publications loaded.</p>'
    return "\n".join(
        f'<div class="recent-pub">\n'
        f'<a class="recent-pub-title" href="{e["url"]}">{e["title"]}</a>\n'
        f'<div class="recent-pub-meta"><span class="pub-venue">{e["journal"]}</span>. {e["year"]}.</div>\n'
        f'</div>'
        for e in entries
    )


def pub_stats(pubs, citations=None):
    entries = pubs["entries"]
    n_pub = len([e for e in entries if e.get("kind", "publication") == "publication"])
    bits = [f'<span id="pubCount">{n_pub} publications</span>']
    asof = ''
    totals = (citations or {}).get("totals")
    if totals and totals.get("citations"):
        bits += [f'{totals["citations"]:,} citations',
                 f'h-index {totals["h_index"]}',
                 f'i10-index {totals["i10_index"]}']
        asof = (f'<br><span class="pub-stats-asof">as of {citations.get("as_of", "")} '
                f'({citations.get("source", "")})</span>')
    return '<p class="pub-stats">' + ' &nbsp;&middot;&nbsp; '.join(bits) + asof + '</p>'


def pub_list_grouped(entries, cites):
    """Year-grouped list of .pub-entry blocks with <h3 class="pub-year"> headers."""
    if not entries:
        return '<p class="pub-empty">No publications loaded.</p>'
    out, cur_year = [], None
    for e in entries:
        if e["year"] != cur_year:
            cur_year = e["year"]
            out.append(f'<h3 class="pub-year" data-year="{cur_year}">{cur_year or "In press"}</h3>')
        out.append(render_pub(e, cites))
    return "\n".join(out)


KIND_LABELS = [("publication", "Publications"), ("abstract", "Abstracts"), ("other", "Other")]


def publications_block(pubs, citations=None):
    entries = pubs["entries"]
    cites = (citations or {}).get("by_id", {})

    kinds = (
        '<div class="pub-kinds" role="group" aria-label="Publication types">\n'
        + "\n".join(
            f'<button type="button" class="pub-kind-toggle{" active" if k == "publication" else ""}" '
            f'data-kind="{k}" aria-pressed="{"true" if k == "publication" else "false"}">{label}</button>'
            for k, label in KIND_LABELS
        )
        + '\n</div>\n'
    )
    search = ('<input type="text" class="pub-search" id="pubSearch" '
              'placeholder="Filter by title, author, journal, or year…" '
              'aria-label="Filter publications">\n')
    body = pub_list_grouped(entries, cites)
    return kinds + search + f'<div class="pub-list" id="pubList">\n{body}\n</div>'


# ---------------------------------------------------------------- search ------
class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def build_search_index(pages_meta):
    idx = []
    for meta in pages_meta:
        html = (OUT / meta["page_file"]).read_text()
        tx = TextExtractor()
        tx.feed(html)
        idx.append(
            {"title": meta["search_title"], "url": meta["page_file"], "content": tx.text()[:4000]}
        )
    (OUT / "assets" / "search.json").write_text(json.dumps(idx, ensure_ascii=False))


# ---------------------------------------------------------------- favicon -----
# the browser tab shows the navbar brand mark: off-white initials on the accent
# tile, the same in both color schemes (a tab icon can't track the theme toggle).
FAVICON_FG = "#f9fafb"


def brand_initials(site):
    words = [w for w in re.split(r"\s+", site["short_name"]) if w]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    parts = re.findall(r"[A-Za-z]", site["short_name"])
    return (parts[0] + (parts[1] if len(parts) > 1 else "")).upper() if parts else "CM"


def write_favicon(site):
    """Standalone favicon.svg for the browser tab: the navbar brand mark —
    off-white initials on the accent tile — identical in light and dark."""
    initials = brand_initials(site)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{site["accent"]}"/>'
        '<text x="32" y="44" font-family="Georgia,serif" font-size="28" '
        f'font-weight="700" text-anchor="middle" fill="{FAVICON_FG}">{initials}</text></svg>'
    )
    (OUT / "favicon.svg").write_text(svg)


def brand_icon_svg(site):
    """Inline SVG for the navbar brand. Uses CSS custom properties so it tracks
    the live data-bs-theme toggle, not just the OS setting."""
    initials = brand_initials(site)
    return (
        '<svg class="brand-icon" width="24" height="24" viewBox="0 0 64 64" '
        'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        '<rect width="64" height="64" rx="14" fill="var(--accent)"/>'
        '<text x="32" y="44" font-family="Georgia,serif" font-size="28" font-weight="700" '
        f'text-anchor="middle" fill="var(--bg-primary)">{initials}</text></svg>'
    )


# ---------------------------------------------------------------- main --------
def parse_front_matter(raw):
    m = FRONT.match(raw)
    meta = {}
    if m:
        for pair in m.group(1).split("|"):
            if ":" in pair:
                k, v = pair.split(":", 1)
                meta[k.strip()] = v.strip()
        raw = raw[m.end():]
    return meta, raw


def main():
    site = json.loads((SRC / "site.json").read_text())
    members, alumni = load_team(SRC / "data" / "team.txt")

    pub_path = SRC / "data" / "publications.json"
    if pub_path.exists():
        pubs = json.loads(pub_path.read_text())
    else:
        pubs = {"count": 0, "entries": []}
        print("build.py: WARNING: src/data/publications.json missing — run "
              "`python3 update_publications.py` first.", file=sys.stderr)

    cite_path = SRC / "data" / "citations.json"
    if cite_path.exists():
        citations = json.loads(cite_path.read_text())
    else:
        citations = None
        print("build.py: note: src/data/citations.json missing — publications render "
              "without citation counts. Run `python3 fetch_citations.py` to add them.",
              file=sys.stderr)

    # fresh output
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(SRC / "assets", OUT / "assets")

    # cache-busting token for main.css / site.js (they change often; no query
    # string means browsers serve a stale copy after every edit)
    import hashlib
    h = hashlib.sha1()
    for f in ("css/main.css", "js/site.js"):
        h.update((OUT / "assets" / f).read_bytes())
    asset_v = h.hexdigest()[:8]

    write_favicon(site)

    enabled_sameas = [s["href"] for s in site["socials"]
                      if s.get("href") and not s.get("disabled")
                      and not s["href"].startswith("mailto:")]
    jsonld_sameas = (', "sameAs": [' + ", ".join(f'"{u}"' for u in enabled_sameas) + "]") if enabled_sameas else ""

    base_ctx = {
        "name": site["name"],
        "short_name": site["short_name"],
        "title": site["title"],
        "institution": site["institution"],
        "email": site["email"],
        "url": site["url"].rstrip("/"),
        "photo": site["photo"],
        "accent": site["accent"],
        "accent_hover": site["accent_hover"],
        "year": str(date.today().year),
        "address_html": "<br>".join(site["address_lines"]),
        "phone": site.get("phone", ""),
        "lab_phone": site.get("lab_phone", ""),
        "fax": site.get("fax", ""),
        "admin_name": site.get("admin_name", ""),
        "admin_email": site.get("admin_email", ""),
        "pi_name": site.get("pi_name", ""),
        "pi_profile": site.get("pi_profile", ""),
        "pub_count": str(len([e for e in pubs["entries"]
                              if e.get("kind", "publication") == "publication"])),
        "jsonld_sameas": jsonld_sameas,
        "asset_v": asset_v,
        "_inc": {
            "navlinks": "",  # placeholder, set per page
            "brand_icon": brand_icon_svg(site),
            "profile_links": profile_links(site),
            "publications": publications_block(pubs, citations),
            "pub_stats": pub_stats(pubs, citations),
            "recent_publications": recent_publications(pubs, limit=5),
            "team_grid": team_grid(members),
            "alumni_box": alumni_box(alumni),
        },
    }

    shell = (SRC / "partials" / "_page.html").read_text()
    pages = sorted((SRC / "pages").glob("*.html"))
    if not pages:
        die("no pages in src/pages/")

    pages_meta = []
    active_seen = set()
    for page_path in pages:
        raw = page_path.read_text()
        meta, body_src = parse_front_matter(raw)
        page_file = page_path.name
        active = meta.get("active", "")
        active_seen.add(active)
        page_title = meta.get("title", "")
        desc = meta.get("desc", site["description"])
        is_home = active == "home"

        ctx = dict(base_ctx)
        ctx["_inc"] = dict(base_ctx["_inc"])
        ctx["_inc"]["navlinks"] = nav_links(site, active)
        ctx["page_file"] = page_file
        ctx["page_title"] = page_title
        ctx["page_desc"] = desc
        ctx["active"] = active
        ctx["head_title"] = site["name"] if is_home else f"{page_title} · {site['name']}"
        ctx["og_title"] = site["name"] if is_home else page_title
        ctx["body_class"] = f"page-{active}"

        if is_home:
            body = (
                '<div class="home-grid">\n<div class="home-content">\n'
                + body_src
                + '\n</div>\n<div>\n'
                + render((SRC / "partials" / "sidebar.html").read_text(), ctx, 1)
                + "\n</div>\n</div>"
            )
        else:
            # No page-level fade wrapper: a single element taller than the
            # viewport can never satisfy an IntersectionObserver and would
            # leave the whole page stuck at opacity 0. Instead the top-level
            # card blocks each animate in on scroll (.pub-entry already does).
            body_src = re.sub(
                r'<div class="(section-card|research-card|card-academic)"',
                r'<div class="\1 fade-in-section"',
                body_src,
            )
            body = f'<div class="page-body">\n{body_src}\n</div>'
        ctx["_inc"]["body"] = body

        html = render(shell, ctx)
        stray = TOKEN.search(html)
        if stray:
            die(f"unresolved token {stray.group(0)} in {page_file}")
        (OUT / page_file).write_text(html)
        pages_meta.append({
            "page_file": page_file,
            "search_title": page_title or site["short_name"],
        })

    # asserts
    for item in site["nav"]:
        if item["id"] not in active_seen:
            die(f'nav item "{item["id"]}" has no page with active: {item["id"]}')

    # per-member publication pages (not in nav)
    cites = (citations or {}).get("by_id", {})
    member_tmpl = (SRC / "partials" / "member.html").read_text()
    n_members = 0
    for m in members:
        if not m["match"]:
            continue
        needle = m["match"].lower()
        mine = [e for e in pubs["entries"]
                if e.get("kind", "publication") in ("publication", "abstract")
                and needle in e.get("authors_plain", "").lower()]
        page_file = f'member-{m["slug"]}.html'
        ctx = dict(base_ctx)
        ctx["_inc"] = dict(base_ctx["_inc"])
        ctx["_inc"]["navlinks"] = nav_links(site, "team")
        ctx["member_publications"] = (
            pub_list_grouped(mine, cites) if mine
            else '<p class="pub-empty">No publications found under this name yet.</p>')
        ctx["member_name"] = m["name"]
        ctx["member_title"] = m["title"]
        ctx["member_photo"] = m["photo"]
        ctx["member_links"] = member_links_html(m)
        ctx["page_file"] = page_file
        ctx["page_title"] = m["name"]
        ctx["page_desc"] = f'{m["name"]} — {m["title"]}, {site["name"]}.'
        ctx["active"] = "team"
        ctx["head_title"] = f'{m["name"]} · {site["name"]}'
        ctx["og_title"] = m["name"]
        ctx["body_class"] = "page-member"
        body_src = re.sub(r'<div class="(section-card)"', r'<div class="\1 fade-in-section"',
                          render(member_tmpl, ctx, 1))
        ctx["_inc"]["body"] = f'<div class="page-body">\n{body_src}\n</div>'
        html = render(shell, ctx)
        stray = TOKEN.search(html)
        if stray:
            die(f"unresolved token {stray.group(0)} in {page_file}")
        (OUT / page_file).write_text(html)
        pages_meta.append({"page_file": page_file, "search_title": m["name"]})
        n_members += 1

    build_search_index(pages_meta)

    print(f"built {len(pages_meta)} pages -> {OUT}")
    print(f"  publications: {len(pubs['entries'])}  (generated {pubs.get('generated', '?')})")
    print(f"  team: {len(members)} members ({n_members} with pages), {len(alumni)} alumni")

    if "--serve" in sys.argv:
        import functools
        import http.server
        import socketserver

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
        for port in range(8000, 8011):
            try:
                httpd = socketserver.TCPServer(("", port), handler)
            except OSError:
                continue
            print(f"serving http://localhost:{port}  (Ctrl-C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")
            httpd.server_close()
            break
        else:
            die("ports 8000-8010 are all busy — free one with: lsof -ti tcp:8000 | xargs kill")


if __name__ == "__main__":
    main()
