#!/usr/bin/env python3
"""Regenerate the Ley Lab publication list.

    python3 update_publications.py             # write src/data/publications.json
    python3 update_publications.py --dry-run   # show the diff, write nothing

Pipeline:
    PubMed  esearch "Ley TJ[Author]"  ->  PMID list  ->  efetch (batched)
        title / authors / journal / year / DOI / publication types
    OpenAlex  works by author A5076598094, type:conference-abstract | erratum
        the ASH / AACR meeting abstracts etc. that PubMed does not index
    + src/data/pubmed_overrides.txt  (exclude / include / reclassify by PMID)
    classify each record as  publication | abstract | other
    sort newest first  ->  src/data/publications.json

Only "publication" shows by default on the site; the Abstracts / Other buttons
reveal the rest. Google Scholar can't be scraped, so sanity-check against it by
hand and add any missing PMIDs via `include` lines in pubmed_overrides.txt.

Stdlib only; needs network.
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

SEARCH_TERM = "Ley TJ[Author]"
PI_MATCH = ("Ley", "TJ")  # (LastName, Initials) to render in bold
JSON_OUT = Path("src/data/publications.json")
OVERRIDES = Path("src/data/pubmed_overrides.txt")
# Hand-curated records that neither PubMed nor the OpenAlex sweep produce — mostly
# older meeting abstracts and author corrections found by scripts/build_extra_publications.py
# (a one-off Google Scholar reconciliation). Committed; safe to hand-edit.
EXTRA = Path("src/data/extra_publications.json")

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BATCH = 200

# OpenAlex: pull work types PubMed doesn't index (meeting abstracts, errata).
# A5076598094 = Timothy J. Ley  (https://openalex.org/A5076598094)
OPENALEX = "https://api.openalex.org/works"
OPENALEX_AUTHOR_ID = "A5076598094"
MAILTO = "brittniblack@wustl.edu"
OPENALEX_EXTRA_TYPES = {"conference-abstract": "abstract", "erratum": "other"}

# PubMed publication types that mean "not a primary publication"
# "Letter" is deliberately NOT here — many journals file primary research letters
# under it. Comments / replies / errata are genuinely not primary publications.
OTHER_TYPES = {
    "Published Erratum", "Comment", "Editorial", "News",
    "Retraction of Publication", "Retracted Publication",
    "Expression of Concern", "Biography", "Newspaper Article",
}
ABSTRACT_TYPES = {"Meeting Abstract", "Congress", "Overall"}
ABSTRACT_TITLE_RE = re.compile(r"\b(abstract|corrigend|erratum|correction)\b", re.I)


# ----------------------------------------------------------------- overrides ---
def load_overrides():
    """-> (excl, incl, kinds).  excl is a set of PMIDs and/or bare DOIs (lowercased);
    incl is a list of PMIDs to add; kinds maps a PMID or bare DOI -> forced kind."""
    excl, incl, kinds = set(), [], {}
    if not OVERRIDES.exists():
        return excl, incl, kinds
    for raw in OVERRIDES.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "exclude" and len(parts) == 2:
            excl.add(parts[1].lower())
        elif parts[0] == "include" and len(parts) == 2:
            incl.append(parts[1])
        elif parts[0] == "kind" and len(parts) == 3:
            kinds[parts[1].lower()] = parts[2]
        else:
            print(f"WARNING: unrecognized override line: {raw!r}")
    return excl, incl, kinds


def override_key(e):
    """The id an overrides line would use for this entry: its PMID, else bare DOI."""
    return e["pmid"] if e.get("pmid") else _bare_doi(e.get("doi"))


def norm_title(t):
    t = html.unescape(t or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_extra(have_dois, have_pmids, have_titles):
    """Read src/data/extra_publications.json, dropping anything already covered."""
    if not EXTRA.exists():
        return []
    out, seen_d, seen_t = [], set(have_dois), set(have_titles)
    for raw in json.loads(EXTRA.read_text()).get("entries", []):
        d = _bare_doi(raw.get("doi"))
        nt = norm_title(raw["title"])
        if (d and d in seen_d) or (raw.get("pmid") and raw["pmid"] in have_pmids) or nt in seen_t:
            continue
        if d:
            seen_d.add(d)
        seen_t.add(nt)
        e = {k: v for k, v in raw.items() if not k.startswith("_")}  # drop scrape metadata
        e.setdefault("kind", "abstract")
        y = re.match(r"(\d{4})", str(e.get("year", "")))
        e["_sortdate"] = int(y.group(1)) * 10000 if y else 0
        out.append(e)
    return out


# -------------------------------------------------------------------- pubmed ---
def esearch(term):
    url = (f"{ESEARCH}?db=pubmed&retmode=json&retmax=1000&term="
           + urllib.parse.quote(term))
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    return data["esearchresult"]["idlist"]


def efetch(pmids):
    articles = {}
    for i in range(0, len(pmids), BATCH):
        group = pmids[i:i + BATCH]
        url = f"{EFETCH}?db=pubmed&retmode=xml&id=" + ",".join(group)
        with urllib.request.urlopen(url, timeout=120) as r:
            root = ET.fromstring(r.read())
        for art in root.findall("PubmedArticle"):
            pmid = art.findtext(".//MedlineCitation/PMID")
            if pmid:
                articles[pmid] = art
        if i + BATCH < len(pmids):
            time.sleep(0.5)
    return articles


# ------------------------------------------------------------------ parsing ---
def cap_each(s):
    return " ".join(w.capitalize() for w in s.split()).replace("Plos", "PLoS").replace("Bmc", "BMC")


def clean_journal(title):
    t = title.split(" : ")[0]
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    t = cap_each(t)
    t = re.sub(r"^The ", "", t)
    t = re.sub(r" Of The United States Of America$", "", t)
    low = t.lower()
    for pre, fixed in (("biorxiv", "bioRxiv"), ("medrxiv", "medRxiv"), ("arxiv", "arXiv"),
                       ("research square", "Research Square")):
        if low.startswith(pre):
            return fixed
    if low == "nejm evidence":
        return "NEJM Evidence"
    return t


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def node_text(el):
    return "".join(el.itertext()) if el is not None else ""


# short all-caps tokens in a shouty title that are ordinary words, not acronyms
_NOT_ACRONYM = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BY", "FOR", "FROM", "IN", "INTO", "IS",
    "IT", "ITS", "NEW", "NON", "OF", "ON", "OR", "PER", "PRE", "POST", "THE", "TO",
    "TWO", "VIA", "VS", "WAS", "WITH", "HUMAN", "MOUSE", "NORMAL", "GENE", "GENES",
    "CELL", "CELLS", "ANTI", "AFTER", "DURING", "LINE", "ALPHA", "BETA", "GAMMA",
    "DELTA", "TYPE", "ROLE", "LOSS", "SITE", "SITES",
}


def _fix_caps_token(w, first):
    """Fix one space-delimited token, recursing into hyphenated parts."""
    if "-" in w and any(c.isalpha() for c in w.replace("-", "")):
        parts = w.split("-")
        return "-".join(_fix_caps_token(p, first and i == 0) for i, p in enumerate(parts))
    core = re.sub(r"[^A-Za-z0-9]", "", w)
    letters = re.sub(r"[^A-Za-z]", "", core)
    if letters and not letters.isupper() and not letters.islower():
        return w  # already mixed-case (mRNA, pVACtools) — leave it
    vowels = sum(c in "AEIOU" for c in letters)
    keep = (
        core.isupper() and core not in _NOT_ACRONYM and len(core) >= 2 and (
            (any(c.isdigit() for c in core) and len(letters) <= 3)   # U937, TP53, HS-2
            or (len(core) <= 5 and vowels <= 1)                       # DNA, AML, TIMP, HPFH
        )
    )
    if keep:
        return w
    if first:
        return w[:1].upper() + w[1:].lower()
    return w.lower()


def fix_caps(title):
    """Sentence-case a SHOUTING title, keeping obvious acronyms / gene symbols.
    A no-op unless most alphabetic words are all-caps."""
    words = title.split()
    alpha = [w for w in words if any(c.isalpha() for c in w)]
    if len(alpha) < 3 or sum(1 for w in alpha if w == w.upper()) / len(alpha) < 0.65:
        return title
    s = " ".join(_fix_caps_token(w, i == 0) for i, w in enumerate(words))
    return re.sub(r"([.:]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)


def sortdate_from_pubdate(art):
    """YYYYMMDD int from a PubMed <PubDate>, for stable newest-first ordering."""
    pd = art.find(".//Article/Journal/JournalIssue/PubDate")
    y = m = d = 0
    if pd is not None:
        yt = pd.findtext("Year")
        if yt and yt.isdigit():
            y = int(yt)
        else:
            mm = re.search(r"(19|20)\d\d", pd.findtext("MedlineDate") or "")
            y = int(mm.group(0)) if mm else 0
        m = _MONTHS.get((pd.findtext("Month") or "")[:3], 0)
        d = int(pd.findtext("Day") or 0) if (pd.findtext("Day") or "").isdigit() else 0
    return y * 10000 + m * 100 + d


def name_to_last_init(display):
    """'Michael D. Bern' -> ('Bern', 'MD', 'Bern MD');  'de Jesus R' kept as-is-ish."""
    toks = [t for t in re.split(r"\s+", display.strip()) if t]
    if len(toks) < 2:
        return None, None, display.strip()
    last = toks[-1].rstrip(".")
    if last.lower() in {"jr", "sr", "ii", "iii", "iv"} and len(toks) >= 3:
        last = toks[-2].rstrip(".") + " " + last
        given = toks[:-2]
    else:
        given = toks[:-1]
    init = "".join(t[0].upper() for t in given if t and t[0].isalpha())
    return last, init, f"{last} {init}".strip()


def authors_from_pubmed(art):
    al = art.find(".//Article/AuthorList")
    if al is None:
        return []
    people, collectives = [], []
    for a in al.findall("Author"):
        coll = a.findtext("CollectiveName")
        if coll:
            c = coll.strip().rstrip(".").strip()
            if "@" in c or c.lower().startswith("electronic address"):
                continue
            if c not in collectives:
                collectives.append(c)
            continue
        last = a.findtext("LastName")
        if not last:
            continue
        init = a.findtext("Initials") or ""
        people.append({"last": last, "init": init,
                       "name": f"{last} {init}".strip(),
                       "eq": a.get("EqualContrib") == "Y"})
    if people:
        return people
    return [{"last": None, "init": None, "name": c, "eq": False} for c in collectives]


def is_pi(a):
    return a["last"] == PI_MATCH[0] and (a["init"] or "").startswith(PI_MATCH[1][0])


def fmt_authors(authors):
    def render(a):
        star = "*" if a["eq"] else ""
        if a["last"] and is_pi(a):
            return f"<strong>{PI_MATCH[0]} {PI_MATCH[1]}{star}</strong>"
        return a["name"] + star
    shown = authors[:10]
    names = [render(a) for a in shown]
    if len(authors) > 10:
        if not any(a["last"] and is_pi(a) for a in shown):
            pi = next((a for a in authors if a["last"] and is_pi(a)), None)
            if pi:
                names.append(render(pi))
        names.append("<em>et al.</em>")
    return ", ".join(names)


def pub_types(art):
    return {node_text(pt).strip() for pt in art.findall(".//Article/PublicationTypeList/PublicationType")}


def classify(title, types, override):
    if override:
        return override
    if types & OTHER_TYPES:
        return "other"
    if types & ABSTRACT_TYPES or ABSTRACT_TITLE_RE.search(title):
        return "abstract"
    return "publication"


def build_entry(pmid, art, kind_override):
    title = node_text(art.find(".//Article/ArticleTitle")).strip().rstrip(".").strip()
    journal = clean_journal(art.findtext(".//Article/Journal/Title") or "")
    year = art.findtext(".//Article/Journal/JournalIssue/PubDate/Year")
    if not year:
        md = art.findtext(".//Article/Journal/JournalIssue/PubDate/MedlineDate") or ""
        m = re.search(r"(19|20)\d\d", md)
        year = m.group(0) if m else ""
    doi = None
    for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = aid.text
    if not doi:
        el = art.find('.//Article/ELocationID[@EIdType="doi"]')
        doi = el.text if el is not None else None
    doi = (doi or "").strip().rstrip(".")

    authors = authors_from_pubmed(art)
    types = pub_types(art)
    kind = classify(title, types, kind_override)

    if doi:
        href, link_kind = f"https://doi.org/{doi}", "doi"
    else:
        href, link_kind = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "pubmed"

    return {
        "n": 0,
        "year": year,
        "title": title.replace("&", "&amp;"),
        "authors_html": fmt_authors(authors),
        "authors_plain": ", ".join(a["name"] for a in authors),
        "journal": journal,
        "doi": doi,
        "url": href,
        "link_kind": link_kind,
        "pmid": pmid,
        "kind": kind,
        "_sortdate": sortdate_from_pubdate(art),
    }


# ------------------------------------------------------------------ openalex ---
def _bare_doi(doi):
    if not doi:
        return ""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I).lower()


def openalex_extra_entries(have_dois, have_pmids):
    """Meeting abstracts / errata from OpenAlex that PubMed doesn't index."""
    types = "|".join(OPENALEX_EXTRA_TYPES)
    out, cursor = [], "*"
    while cursor:
        url = (f"{OPENALEX}?filter=authorships.author.id:{OPENALEX_AUTHOR_ID},"
               f"type:{types}&per-page=200&cursor={urllib.parse.quote(cursor)}"
               "&select=id,doi,ids,title,type,publication_year,publication_date,"
               "primary_location,authorships&mailto=" + MAILTO)
        with urllib.request.urlopen(url, timeout=90) as r:
            page = json.load(r)
        for w in page.get("results", []):
            doi = _bare_doi(w.get("doi"))
            pmid = ""
            m = re.search(r"/(\d+)$", (w.get("ids") or {}).get("pmid", "") or "")
            if m:
                pmid = m.group(1)
            if (doi and doi in have_dois) or (pmid and pmid in have_pmids):
                continue  # already covered by the PubMed list
            title = (w.get("title") or "").strip().rstrip(".").strip()
            if not title:
                continue
            src = (w.get("primary_location") or {}).get("source") or {}
            journal = clean_journal(src.get("display_name") or "")
            authors = []
            for a in w.get("authorships", []):
                disp = a.get("author", {}).get("display_name") or a.get("raw_author_name") or ""
                last, init, name = name_to_last_init(disp)
                authors.append({"last": last, "init": init, "name": name, "eq": False})
            pdate = w.get("publication_date") or ""
            sd = 0
            mm = re.match(r"(\d{4})-(\d{2})-(\d{2})", pdate)
            if mm:
                sd = int(mm.group(1)) * 10000 + int(mm.group(2)) * 100 + int(mm.group(3))
            elif w.get("publication_year"):
                sd = int(w["publication_year"]) * 10000
            href = f"https://doi.org/{doi}" if doi else w["id"]
            out.append({
                "n": 0,
                "year": str(w.get("publication_year") or (sd // 10000) or ""),
                "title": title.replace("&", "&amp;"),
                "authors_html": fmt_authors(authors),
                "authors_plain": ", ".join(a["name"] for a in authors),
                "journal": journal,
                "doi": doi,
                "url": href,
                "link_kind": "doi" if doi else "openalex",
                "pmid": pmid,
                "kind": OPENALEX_EXTRA_TYPES[w["type"]],
                "_sortdate": sd,
            })
        cursor = (page.get("meta") or {}).get("next_cursor")
    return out


# --------------------------------------------------------------------- main ---
def sort_key(e):
    return (e.get("_sortdate", 0), int(e["pmid"] or 0))


def main():
    dry = "--dry-run" in sys.argv[1:]
    excl, incl, kinds = load_overrides()

    pmids = esearch(SEARCH_TERM)
    print(f"esearch '{SEARCH_TERM}': {len(pmids)} PMIDs")
    ids = list(dict.fromkeys(pmids + [p for p in incl if p not in pmids]))

    arts = efetch(ids)
    missing = [p for p in ids if p not in arts]
    if missing:
        print(f"WARNING: {len(missing)} PMIDs not returned by efetch: {missing}")

    entries = [build_entry(p, arts[p], None) for p in ids if p in arts]
    print(f"  {len(entries)} from PubMed")

    have_dois = {e["doi"].lower() for e in entries if e.get("doi")}
    have_pmids = {e["pmid"] for e in entries if e.get("pmid")}
    extra = openalex_extra_entries(have_dois, have_pmids)
    print(f"  {len(extra)} extra from OpenAlex (meeting abstracts / errata)")
    entries += extra

    # hand-curated records (src/data/extra_publications.json)
    curated = load_extra(have_dois | {_bare_doi(e.get("doi")) for e in extra if e.get("doi")},
                         have_pmids | {e["pmid"] for e in extra if e.get("pmid")},
                         {norm_title(e["title"]) for e in entries})
    print(f"  {len(curated)} hand-curated (extra_publications.json)")
    entries += curated

    # normalize SHOUTING titles everywhere
    for e in entries:
        e["title"] = fix_caps(e["title"])

    # apply pubmed_overrides.txt (exclude / reclassify by PMID or bare DOI)
    dropped = [e for e in entries if override_key(e) in excl]
    entries = [e for e in entries if override_key(e) not in excl]
    for e in entries:
        forced = kinds.get(override_key(e))
        if forced:
            e["kind"] = forced
    if excl:
        print(f"  overrides: dropped {len(dropped)}, reclassified {len(kinds)}")

    entries.sort(key=sort_key, reverse=True)
    # n counts up with age: newest entry gets the highest number (matches CV site)
    for i, e in enumerate(entries):
        e["n"] = len(entries) - i
        e.pop("_sortdate", None)

    by_kind = {}
    for e in entries:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    print(f"{len(entries)} entries: " + ", ".join(f"{v} {k}" for k, v in sorted(by_kind.items())))

    payload = {
        "generated": date.today().isoformat(),
        "count": len(entries),
        "search_term": SEARCH_TERM,
        "entries": entries,
    }

    def dkey(e):
        return e["doi"].lower() if e.get("doi") else (f'pmid:{e["pmid"]}' if e.get("pmid") else e["url"])

    if JSON_OUT.exists():
        old = {dkey(e): e for e in json.loads(JSON_OUT.read_text()).get("entries", [])}
        new = {dkey(e): e for e in entries}
        added = sorted(set(new) - set(old), key=lambda k: new[k]["year"], reverse=True)
        removed = sorted(set(old) - set(new))
        reclassified = [k for k in set(old) & set(new) if old[k].get("kind") != new[k]["kind"]]
        for k in added:
            print(f"  + {new[k]['year']}  [{new[k]['kind']}]  {new[k]['title'][:68]}")
        for k in removed:
            print(f"  - {old[k].get('year','')}  {old[k]['title'][:68]}")
        for k in reclassified:
            print(f"  ~ {new[k]['title'][:56]}  {old[k].get('kind')} -> {new[k]['kind']}")
        if not (added or removed or reclassified):
            print("  no changes vs current publications.json")

    if dry:
        print("\n--dry-run: publications.json not written")
        return
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    print(f"\nwrote {JSON_OUT}")


if __name__ == "__main__":
    main()
