#!/usr/bin/env python3
"""One-off verification: scrape Timothy Ley's Google Scholar profile and diff it
against the current (PubMed + OpenAlex) publications.json.

This is NOT part of the normal `update_publications.py` flow — Google Scholar
blocks automated access and the `scholarly` dependency is heavyweight. Run it by
hand every year or two to catch anything the automated sources miss.

    python3 -m venv /tmp/scholar-venv
    /tmp/scholar-venv/bin/pip install scholarly
    /tmp/scholar-venv/bin/python scripts/verify_against_scholar.py

Companion scripts:
  scripts/build_extra_publications.py  turns reviewed conflicts into extra_publications.json
  scripts/scholar_classification.tsv   hand decisions for the "Other" bucket
  scripts/scholar_ignore.txt           Scholar ids already handled (hidden here)

Writes:
  scripts/scholar_raw.json   the scraped list (delete it to force a re-scrape)
  publications_unverified.md the diff lists, at the repo root
"""
import json
import re
import sys
import time
import html
import difflib
from pathlib import Path

SCHOLAR_ID = "b9C-eUQAAAAJ"
REPO = Path(__file__).resolve().parent.parent
PUBS = REPO / "src" / "data" / "publications.json"
EXTRA = REPO / "src" / "data" / "extra_publications.json"
RAW = Path(__file__).with_name("scholar_raw.json")
IGNORE = Path(__file__).with_name("scholar_ignore.txt")
OUT = REPO / "publications_unverified.md"


def norm(t):
    t = html.unescape(t or "").lower()
    t = t.replace("‐", "-").replace("–", "-").replace("—", "-")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def scrape():
    if RAW.exists():
        print(f"reusing {RAW.name} (delete it to re-scrape)")
        return json.loads(RAW.read_text())

    from scholarly import scholarly, ProxyGenerator

    # Try direct first; fall back to free proxies if Google throws up a captcha.
    try:
        author = scholarly.search_author_id(SCHOLAR_ID)
        author = scholarly.fill(author, sections=["basics", "indices", "publications"])
    except Exception as e:
        print(f"direct fetch failed ({e!r}); retrying via free proxies…")
        pg = ProxyGenerator()
        if not pg.FreeProxies():
            sys.exit("could not get a working proxy — try again later or from another network")
        scholarly.use_proxy(pg)
        author = scholarly.search_author_id(SCHOLAR_ID)
        author = scholarly.fill(author, sections=["basics", "indices", "publications"])

    pubs = []
    for p in author.get("publications", []):
        b = p.get("bib", {})
        pubs.append({
            "title": b.get("title", ""),
            "year": str(b.get("pub_year", "") or ""),
            "venue": b.get("citation", "") or b.get("journal", "") or "",
            "num_citations": p.get("num_citations", 0),
            "author_pub_id": p.get("author_pub_id", ""),
        })
    data = {
        "name": author.get("name"),
        "scholar_id": SCHOLAR_ID,
        "total_citations": author.get("citedby"),
        "hindex": author.get("hindex"),
        "count": len(pubs),
        "publications": pubs,
    }
    RAW.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"scraped {len(pubs)} publications -> {RAW.name}")
    return data


def best_match(key, index):
    """index: {normkey: entry}.  Returns (entry, score) or (None, 0)."""
    if not key:
        return None, 0.0
    if key in index:
        return index[key], 1.0
    # truncation / prefix-word differences ("Retrospective: X", "Sociology. X"):
    # accept when the shorter normalized title is contained in the longer one
    for k, e in index.items():
        a, b = sorted((key, k), key=len)
        if len(a) >= 28 and a in b:
            return e, 0.96
    m = difflib.get_close_matches(key, index.keys(), n=1, cutoff=0.87)
    if m:
        return index[m[0]], difflib.SequenceMatcher(None, key, m[0]).ratio()
    return None, 0.0


def main():
    sch = scrape()
    pubs = json.loads(PUBS.read_text())
    entries = pubs["entries"]

    ours_index = {norm(e["title"]): e for e in entries}
    sch_index = {norm(p["title"]): p for p in sch["publications"] if p["title"]}

    ignore = set()
    if IGNORE.exists():
        ignore = {ln.strip() for ln in IGNORE.read_text().splitlines()
                  if ln.strip() and not ln.startswith("#")}

    scholar_only, matched_pairs, ignored = [], [], 0
    for p in sch["publications"]:
        if not p["title"]:
            continue
        if p.get("author_pub_id") in ignore:
            ignored += 1
            continue
        e, score = best_match(norm(p["title"]), ours_index)
        if e is None:
            scholar_only.append(p)
        else:
            matched_pairs.append((p, e, score))

    matched_ours = {id(e) for _, e, _ in matched_pairs}
    ours_only = []
    for e in entries:
        e2, score = best_match(norm(e["title"]), sch_index)
        if e2 is None and id(e) not in matched_ours:
            ours_only.append(e)

    # ---- bucket the scholar-only list so it's actionable ----
    ABS_VENUE = re.compile(r"blood|supplement|suppl|cancer res|clinical oncology|haematologica|"
                           r"cancer immunol|j immunol|american society of hematology|aacr|abstract",
                           re.I)
    PATENT = re.compile(r"patent", re.I)
    buckets = {"Meeting abstracts / conference proceedings": [], "Patents": [], "Other": []}
    for p in scholar_only:
        v = p["venue"]
        if PATENT.search(v):
            buckets["Patents"].append(p)
        elif ABS_VENUE.search(v) or ABS_VENUE.search(p["title"]):
            buckets["Meeting abstracts / conference proceedings"].append(p)
        else:
            buckets["Other"].append(p)

    def cite_link(p):
        return (f" · [scholar entry](https://scholar.google.com/citations?view_op=view_citation"
                f"&hl=en&user={SCHOLAR_ID}&citation_for_view={p['author_pub_id']})"
                if p["author_pub_id"] else "")

    # ---- report ----
    L = []
    L.append("# Google Scholar reconciliation\n")
    L.append(f"_One-off scrape of {sch['name']} (`{sch['scholar_id']}`) on "
             f"{time.strftime('%Y-%m-%d')} — {sch['count']} items on Scholar vs "
             f"{len(entries)} in `publications.json` "
             f"({sum(1 for e in entries if e.get('kind')=='publication')} publications / "
             f"{sum(1 for e in entries if e.get('kind')=='abstract')} abstracts / "
             f"{sum(1 for e in entries if e.get('kind')=='other')} other). "
             f"Scholar reports {sch['total_citations']:,} citations"
             + (f", h-index {sch['hindex']}" if sch.get('hindex') else "") + "._\n")
    L.append(f"_{len(matched_pairs)} matched · {len(scholar_only)} Scholar-only · "
             f"{len(ours_only)} ours-only. Matching is by normalized title "
             f"(exact / substring / difflib ≥ 0.87) — a starting point, not gospel._\n")

    L.append(f"\n## In Google Scholar but NOT in publications.json ({len(scholar_only)})\n")
    L.append("Add real ones via `include <pmid>` in `src/data/pubmed_overrides.txt` "
             "(or as an OpenAlex-typed abstract, which the updater already sweeps).\n")
    for name, items in buckets.items():
        if not items:
            continue
        L.append(f"\n### {name} ({len(items)})\n")
        for p in sorted(items, key=lambda x: x["year"] or "", reverse=True):
            L.append(f"- **{p['year'] or '????'}** — {html.unescape(p['title'])}  \n"
                     f"  <sub>{p['venue']} · cited by {p['num_citations']}{cite_link(p)}</sub>")

    L.append(f"\n## In publications.json but NOT in Google Scholar ({len(ours_only)})\n")
    L.append("Usually fine — Scholar merges variants, drops some errata/comments, and "
             "lags. Check for anything that looks like a wrong-author PubMed hit.\n")
    for e in sorted(ours_only, key=lambda x: x["year"], reverse=True):
        ref = f"pmid:{e['pmid']}" if e.get("pmid") else (e.get("doi") or e["url"])
        L.append(f"- **{e['year'] or '????'}** [{e.get('kind','?')}] — {html.unescape(e['title'])}  \n"
                 f"  <sub>{e['journal']} · {ref}</sub>")

    # ---- items pulled in from Scholar that have no PubMed ID (please spot-check) ----
    if EXTRA.exists():
        in_list = {norm(e["title"]) for e in entries}
        ex = json.loads(EXTRA.read_text()).get("entries", [])
        no_pmid = [e for e in ex if not e.get("_pmid_found") and norm(e["title"]) in in_list]
        L.append(f"\n## Added from Scholar without a PubMed ID — please review ({len(no_pmid)})\n")
        L.append("Pulled into `publications.json` via `src/data/extra_publications.json` "
                 "(Crossref-verified: a Ley is in the author list) but not found in PubMed. "
                 "Mostly older meeting abstracts. Remove one with an `exclude <doi>` line in "
                 "`src/data/pubmed_overrides.txt`.\n")
        for e in sorted(no_pmid, key=lambda x: str(x.get("year", "")), reverse=True):
            ref = e.get("doi") or e.get("url", "")
            L.append(f"- **{e.get('year', '????')}** [{e['kind']}] — {html.unescape(e['title'])}  \n"
                     f"  <sub>{e.get('journal', '')} · {ref}</sub>")

    OUT.write_text("\n".join(L) + "\n")
    print(f"\nwrote {OUT}")
    print(f"  scholar-only: {len(scholar_only)}  (+{ignored} ignored)")
    print(f"  ours-only:    {len(ours_only)}")
    print(f"  matched:      {len(matched_pairs)}")


if __name__ == "__main__":
    main()
