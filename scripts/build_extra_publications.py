#!/usr/bin/env python3
"""One-off: turn the Google Scholar reconciliation into src/data/extra_publications.json.

Reads:
  scripts/scholar_raw.json          the scraped Scholar profile (verify_against_scholar.py)
  scripts/scholar_classification.tsv  hand review of the "Other" bucket
  src/data/publications.json         current list (to skip what's already covered)

For every Scholar item that is (a) in the meeting-abstract bucket or (b) marked
abstract/other in the TSV, it looks the record up in Crossref, keeps it only if a
Ley is in the author list, pulls a PubMed ID if one exists, and emits a full
citation record.

Writes:
  src/data/extra_publications.json   merged by update_publications.py
  scripts/scholar_ignore.txt         Scholar ids to hide from future publications_unverified.md

    python3 scripts/build_extra_publications.py
"""
import json
import re
import sys
import time
import html
import difflib
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import update_publications as up  # noqa: E402  (clean_journal, fix_caps, fmt_authors, PI_MATCH)

RAW = Path(__file__).with_name("scholar_raw.json")
TSV = Path(__file__).with_name("scholar_classification.tsv")
IGNORE = Path(__file__).with_name("scholar_ignore.txt")
PUBS = ROOT / "src" / "data" / "publications.json"
OUT = ROOT / "src" / "data" / "extra_publications.json"

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"
OA_AUTHOR_ID = "A5076598094"  # Timothy J. Ley
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
MAILTO = "brittniblack@wustl.edu"
SCHOLAR_ID = "b9C-eUQAAAAJ"

ABS_VENUE = re.compile(r"blood|supplement|suppl|cancer res|clinical oncology|haematologica|"
                       r"cancer immunol|j immunol|american society of hematology|aacr|abstract",
                       re.I)
PATENT = re.compile(r"patent", re.I)


def norm(t):
    t = html.unescape(t or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def load_tsv():
    """-> {author_pub_id: (decision, doi_or_empty)}"""
    m = {}
    if TSV.exists():
        for line in TSV.read_text().splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                continue
            parts = [c.strip() for c in line.split("\t")]
            if len(parts) >= 2 and parts[0]:
                doi = parts[2] if len(parts) >= 3 and re.match(r"10\.\d{4,}/", parts[2]) else ""
                m[parts[0]] = (parts[1], doi)
    return m


def scholar_only(sch, pubs):
    ours = {norm(e["title"]): e for e in pubs}
    def matched(k):
        if not k or k in ours:
            return True
        for o in ours:
            a, b = sorted((k, o), key=len)
            if len(a) >= 28 and a in b:
                return True
        return bool(difflib.get_close_matches(k, ours.keys(), n=1, cutoff=0.87))
    return [p for p in sch["publications"] if p["title"] and not matched(norm(p["title"]))]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"leylab.org (mailto:{MAILTO})"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def openalex_index():
    """All of Ley's OpenAlex works, keyed by normalized title. Authoritative for
    'is this a Ley paper' and has clean authors/venue/PMID."""
    idx, cursor = {}, "*"
    while cursor:
        url = (f"{OPENALEX}?filter=authorships.author.id:{OA_AUTHOR_ID}&per-page=200"
               "&select=id,doi,ids,title,display_name,publication_year,publication_date,"
               f"type,biblio,primary_location,authorships&cursor={urllib.parse.quote(cursor)}"
               f"&mailto={MAILTO}")
        page = get(url)
        for w in page.get("results", []):
            t = w.get("title") or w.get("display_name")
            if t:
                idx.setdefault(norm(t), w)
        cursor = (page.get("meta") or {}).get("next_cursor")
    return idx


def oa_match(title, idx):
    k = norm(title)
    if k in idx:
        return idx[k]
    m = difflib.get_close_matches(k, idx.keys(), n=1, cutoff=0.82)
    if m:
        return idx[m[0]]
    for ik, w in idx.items():
        a, b = sorted((k, ik), key=len)
        if len(a) >= 32 and a in b:
            return w
    return None


def crossref_by_doi(doi):
    try:
        return get(f"{CROSSREF}/{urllib.parse.quote(doi)}?mailto={MAILTO}")["message"]
    except Exception:
        return None


def crossref_match(title):
    url = f"{CROSSREF}?rows=4&query.bibliographic={urllib.parse.quote(title)}&mailto={MAILTO}"
    try:
        items = get(url)["message"]["items"]
    except Exception:
        return None
    for it in items:
        ct = (it.get("title") or [""])[0]
        if difflib.SequenceMatcher(None, norm(ct), norm(title)).ratio() < 0.82:
            continue
        if any((a.get("family") or "").lower() == "ley" for a in it.get("author", [])):
            return it
    return None


def pmid_for(doi):
    if not doi:
        return None
    try:
        r = get(f"{ESEARCH}?db=pubmed&retmode=json&retmax=1&term={urllib.parse.quote(doi + '[AID]')}")
        ids = r["esearchresult"]["idlist"]
        return ids[0] if ids else None
    except Exception:
        return None


def _authors(names):
    out = []
    for disp in names:
        last, init, name = up.name_to_last_init(disp)
        out.append({"last": last, "init": init, "name": name, "eq": False})
    return out


def meta_from_openalex(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    names = [a["author"].get("display_name") or a.get("raw_author_name") or ""
             for a in w.get("authorships", [])]
    pm = re.search(r"/(\d+)$", (w.get("ids") or {}).get("pmid", "") or "")
    return dict(doi=(up._bare_doi(w.get("doi")) or ""), authors=_authors(names),
                container=src.get("display_name") or "", title=w.get("title") or "",
                year=str(w.get("publication_year") or ""), pmid=pm.group(1) if pm else "",
                oa_type=w.get("type") or "", issue=(w.get("biblio") or {}).get("issue") or "")


ABS_DOI = re.compile(r"^10\.1182/blood[.-]|^10\.1158/1538-7445|^10\.1200/jco\.\d+\.\d+_suppl", re.I)
ABS_PAGES = re.compile(r"\b(\d+)[a-z]?\.\1\b|[Ss]uppl|\bA\d+\b|\b\d+[ab]\b")


def looks_like_abstract(p, meta):
    """Confirm the matched record really is a meeting abstract, not the paper it
    grew into (Scholar lists both under 'Blood 1xx (yy)')."""
    if meta.get("oa_type") == "conference-abstract":
        return True
    if "suppl" in str(meta.get("issue", "")).lower():
        return True
    if ABS_DOI.search(meta.get("doi", "")):
        return True
    if re.search(r"suppl|abstract|meeting", p["venue"], re.I) or ABS_PAGES.search(p["venue"]):
        # ...but a real journal-article with a PMID and a normal issue is the paper
        if meta.get("pmid") and meta.get("oa_type") in ("article", "review") \
                and "suppl" not in str(meta.get("issue", "")).lower():
            return False
        return True
    return False


def meta_from_crossref(it):
    names = []
    for a in it.get("author", []):
        if a.get("family"):
            names.append(f'{a.get("given", "")} {a["family"]}'.strip())
        elif a.get("name"):
            names.append(a["name"])
    year = ""
    for k in ("published-print", "published-online", "published", "issued"):
        dp = (it.get(k) or {}).get("date-parts") or [[]]
        if dp[0]:
            year = str(dp[0][0]); break
    return dict(doi=(it.get("DOI") or "").lower(), authors=_authors(names),
                container=(it.get("container-title") or [""])[0],
                title=(it.get("title") or [""])[0], year=year, pmid="")


def build_record(p, decision, meta, pmid):
    doi = meta["doi"]
    title = up.fix_caps(html.unescape(meta["title"] or p["title"])).strip().rstrip(".")
    year = meta["year"] or (re.search(r"(19|20)\d\d", p["year"] or "") or [""])[0] or p["year"]
    url = (f"https://doi.org/{doi}" if doi else
           f"https://scholar.google.com/citations?view_op=view_citation&hl=en&user={SCHOLAR_ID}"
           f"&citation_for_view={p['author_pub_id']}")
    return {
        "n": 0,
        "year": str(year),
        "title": title.replace("&", "&amp;"),
        "authors_html": up.fmt_authors(meta["authors"]) if meta["authors"] else "",
        "authors_plain": ", ".join(a["name"] for a in meta["authors"]),
        "journal": up.clean_journal(meta["container"]) if meta["container"] else p["venue"],
        "doi": doi,
        "url": url,
        "link_kind": "doi" if doi else "scholar",
        "pmid": pmid or "",
        "kind": decision,
        "_source": f"scholar-scrape {date.today().isoformat()}",
        "_scholar_id": p["author_pub_id"],
        "_pmid_found": bool(pmid),
        "_via": meta.get("via", "?"),
    }


def main():
    sch = json.loads(RAW.read_text())
    pubs = json.loads(PUBS.read_text())["entries"]
    tsv = load_tsv()
    only = scholar_only(sch, pubs)
    print(f"{len(only)} Scholar-only items")
    print("fetching Ley's full OpenAlex works list…")
    oa_idx = openalex_index()
    print(f"  {len(oa_idx)} OpenAlex works indexed")

    records, could_not_verify, leave, ignore_ids, possible_papers = [], [], [], [], []
    for p in only:
        pid = p["author_pub_id"]
        row = tsv.get(pid)                     # (decision, doi) or None
        dec, doi_hint = (row if row else (None, ""))
        from_tsv = row is not None
        if dec is None:
            if PATENT.search(p["venue"]):
                dec = "ignore"
            elif ABS_VENUE.search(p["venue"]) or ABS_VENUE.search(p["title"]):
                dec = "abstract"
            else:
                dec = "leave"

        if dec == "ignore":
            ignore_ids.append(pid)
            continue
        if dec == "leave":
            leave.append(p)
            continue

        meta = None
        if doi_hint:
            it = crossref_by_doi(doi_hint)
            if it:
                meta = meta_from_crossref(it); meta["via"] = "doi-hint"
        if meta is None:
            w = oa_match(p["title"], oa_idx)
            if w is not None:
                meta = meta_from_openalex(w); meta["via"] = "openalex"
            else:
                it = crossref_match(p["title"])
                time.sleep(0.2)
                if it is None:
                    could_not_verify.append(p)
                    continue
                meta = meta_from_crossref(it); meta["via"] = "crossref"
        has_ley = any(a["last"] and a["last"].lower() == "ley" for a in meta["authors"])
        if not has_ley and not (from_tsv and doi_hint):
            # trust an explicit TSV row with a DOI even if Crossref's author list is thin
            could_not_verify.append(p)
            continue
        # guard only bucket-defaulted abstracts (a TSV decision is trusted as-is)
        if dec == "abstract" and not from_tsv and not looks_like_abstract(p, meta):
            possible_papers.append((p, meta))
            continue
        pmid = meta["pmid"] or pmid_for(meta["doi"])
        time.sleep(0.15)
        records.append(build_record(p, dec, meta, pmid))
        ignore_ids.append(pid)  # now covered — hide from publications_unverified.md
        print(f"  + [{dec}] {records[-1]['year']} {meta['via']:8} "
              f"{'PMID '+pmid if pmid else 'no-pmid':>9}  {records[-1]['title'][:56]}")

    records.sort(key=lambda e: (e["year"], e["title"]), reverse=True)
    OUT.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "source": "one-off Google Scholar reconciliation (scripts/build_extra_publications.py)",
        "count": len(records),
        "entries": records,
    }, indent=1, ensure_ascii=False) + "\n")

    IGNORE.write_text(
        "# Scholar author_pub_ids to hide from publications_unverified.md: added to the list, "
        "patents, junk, dupes.\n# Regenerated by scripts/build_extra_publications.py.\n"
        + "\n".join(sorted(set(ignore_ids))) + "\n")

    print(f"\nwrote {OUT}  ({len(records)} records: "
          + ", ".join(f"{sum(1 for r in records if r['kind']==k)} {k}" for k in ("abstract", "other"))
          + f", {sum(1 for r in records if not r['_pmid_found'])} without a PMID)")
    print(f"wrote {IGNORE}  ({len(set(ignore_ids))} ids)")
    print(f"\n{len(could_not_verify)} could not be matched in OpenAlex or Crossref (skipped):")
    for p in could_not_verify:
        print(f"   {p['year'] or '????':>5}  {html.unescape(p['title'])[:75]}   [{p['venue'][:30]}]")

    if possible_papers:
        print(f"\n{len(possible_papers)} matched a real article (not an abstract) — "
              f"maybe missing papers, left for review:")
        for p, m in possible_papers:
            print(f"   {m['year'] or '????':>5}  pmid:{m['pmid'] or '-':<9} {m['title'][:64]}")

    print(f"\n{len(leave)} left for the next review pass (still in publications_unverified.md)")


if __name__ == "__main__":
    main()
