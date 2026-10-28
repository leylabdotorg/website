#!/usr/bin/env python3
"""Scan Ley Lab publications for WashU co-authors not on the team roster.
PubMed = author list + dates; OpenAlex = affiliation backfill."""
import json, re, time, urllib.request, sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / ".cache" / "potential_members"
HERE.mkdir(parents=True, exist_ok=True)
XC = HERE / "efetch_cache"; XC.mkdir(exist_ok=True)
OC = HERE / "openalex_cache"; OC.mkdir(exist_ok=True)
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
MAILTO = "chrisamiller@gmail.com"

pubs = json.loads((ROOT / "src/data/publications.json").read_text())
entries = [e for e in pubs["entries"] if e.get("kind", "publication") == "publication"]
print(f"{len(entries)} publications", file=sys.stderr)

team_raw = """
Ley T|Miller C|Bern M|Kramer M|Cunningham R|Baits R|George D|Hoock M|Helton N|
Day R|Katerndahl C|Abel H|Hickman J|Li Y|Yin K|Yin T|Ramakrishnan S|Redemer P|
Maher B|Lo A|Chen D|Smith A|Petti A|Sutton L|LaValle T|Ketkar S|Ferraro F|
Spencer D|Klco J|Cai M|Rogers O|Christensen K|Richardson S
"""
TEAM = set()
for tok in re.split(r"[|\n]", team_raw):
    tok = tok.strip()
    if not tok:
        continue
    last, init = tok.rsplit(" ", 1)
    TEAM.add((last.lower(), init[0].lower()))

def surkey(surname, given):
    return (surname.strip().lower(), (given.strip()[:1] or "").lower())

# "Washington University" plus the other WashU Medical Campus entities a person
# would list instead of (or as well as) the university itself.
WASHU_RE = re.compile(
    r"washington univ|wash\.? univ\b|\bwashu\b|w[au]stl\b|"
    r"siteman|barnes-?jewish|mallinckrodt|mcdonnell genome|genome institute at washington|"
    r"st\.? louis children'?s hospital|children'?s hospital of st\.? louis",
    re.I)
UWSEA_RE = re.compile(r"univ[a-z. ]*? of washington|univ\. wash\b", re.I)

def txt_is_washu(s):
    if not s:
        return False
    if WASHU_RE.search(s):
        return True
    return False

# ---------------- PubMed pass ----------------
def efetch(group):
    key = XC / (group[0] + "_" + str(len(group)) + ".xml")
    if key.exists():
        return key.read_bytes()
    url = f"{EFETCH}?db=pubmed&retmode=xml&id=" + ",".join(group)
    for att in range(4):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            key.write_bytes(data); time.sleep(0.4); return data
        except Exception as ex:
            print(f"  efetch retry {att}: {ex}", file=sys.stderr); time.sleep(3)
    raise RuntimeError("efetch failed")

pmids = [e["pmid"] for e in entries]
arts = {}
B = 150
for i in range(0, len(pmids), B):
    g = pmids[i:i+B]
    root = ET.fromstring(efetch(g))
    for art in root.findall("PubmedArticle"):
        pm = art.findtext(".//MedlineCitation/PMID")
        if pm:
            arts[pm] = art

MONTHS = {m: n for n, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
def sortdate(art):
    pd = art.find(".//Article/Journal/JournalIssue/PubDate")
    y = m = d = 0
    if pd is not None:
        yt = pd.findtext("Year")
        if yt and yt.isdigit(): y = int(yt)
        else:
            mm = re.search(r"(19|20)\d\d", pd.findtext("MedlineDate") or "")
            y = int(mm.group(0)) if mm else 0
        m = MONTHS.get((pd.findtext("Month") or "")[:3], 0)
        dd = pd.findtext("Day") or ""; d = int(dd) if dd.isdigit() else 0
    return (y, m, d)

# ---------------- OpenAlex pass ----------------
def openalex(pmid):
    key = OC / f"{pmid}.json"
    if key.exists():
        try:
            return json.loads(key.read_text())
        except Exception:
            pass
    url = f"https://api.openalex.org/works/pmid:{pmid}?mailto={MAILTO}"
    for att in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.load(r)
            key.write_text(json.dumps(data)); time.sleep(0.15); return data
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                key.write_text("{}"); return {}
            print(f"  oa retry {att} {pmid}: {ex}", file=sys.stderr); time.sleep(2)
        except Exception as ex:
            print(f"  oa retry {att} {pmid}: {ex}", file=sys.stderr); time.sleep(2)
    return {}

# oa_by_paper[pmid] -> { (sur,init): {"has": bool, "washu": bool, "affils": set} }
oa_by_paper = {}
for n, e in enumerate(entries):
    w = openalex(e["pmid"])
    m = {}
    for a in (w.get("authorships") or []):
        disp = (a.get("author") or {}).get("display_name") or a.get("raw_author_name") or ""
        disp = disp.strip()
        if not disp:
            continue
        toks = disp.split()
        sur = toks[-1]; given = toks[0] if len(toks) > 1 else ""
        k = surkey(sur, given)
        insts = a.get("institutions") or []
        raws = a.get("raw_affiliation_strings") or []
        # Only ROR-parsed per-author institutions are trustworthy. Pre-2000
        # records concatenate the whole paper's affiliation footnote onto every
        # author, so raw strings are NOT used for the WashU decision (an author
        # with only a raw string counts as "affiliation unknown").
        has = bool(insts)
        washu = False
        for ins in insts:
            dn = ins.get("display_name") or ""
            if txt_is_washu(dn) or ins.get("ror") == "https://ror.org/01yc7t268":
                washu = True
        rec = m.setdefault(k, {"has": False, "washu": False, "affils": set(), "ids": set()})
        rec["has"] |= has
        rec["washu"] |= washu
        aid = (a.get("author") or {}).get("id")
        if aid:
            rec["ids"].add(aid)
        for ins in insts:
            if ins.get("display_name"):
                rec["affils"].add(ins["display_name"])
        for rs in raws:
            rec["affils"].add(rs)
    oa_by_paper[e["pmid"]] = m
    if n % 40 == 0:
        print(f"  openalex {n}/{len(entries)}", file=sys.stderr)

# ---------------- merge ----------------
people = {}
for e in entries:
    pm = e["pmid"]
    art = arts.get(pm)
    if art is None:
        continue
    sd = sortdate(art)
    if sd[0] == 0:
        sd = (int(e["year"]) if str(e.get("year", "")).isdigit() else 9999, 0, 0)
    oa = oa_by_paper.get(pm, {})
    al = art.find(".//Article/AuthorList")
    if al is None:
        continue
    for a in al.findall("Author"):
        if a.findtext("CollectiveName"):
            continue
        last = a.findtext("LastName")
        fore = a.findtext("ForeName") or a.findtext("Initials") or ""
        if not last:
            continue
        k = surkey(last, fore)
        if k in TEAM:
            continue
        pubmed_affils = [ai.findtext("Affiliation") or "" for ai in a.findall("AffiliationInfo")]
        pubmed_affils = [x for x in pubmed_affils if x]
        pm_has = bool(pubmed_affils)
        pm_washu = any(txt_is_washu(x) for x in pubmed_affils)
        oarec = oa.get(k, {})
        has = pm_has or oarec.get("has", False)
        washu = pm_washu or oarec.get("washu", False)
        allaffils = set(pubmed_affils) | set(oarec.get("affils", set()))

        key = f"{last.lower()}|{k[1]}"
        p = people.setdefault(key, {
            "display": f"{fore} {last}".strip(), "surname": last,
            "has_affil": False, "washu": False,
            "earliest": None, "earliest_washu": None,
            "n_papers": 0, "affils": set(), "years": set(), "ids": set()})
        p["ids"] |= oarec.get("ids", set())
        cand = f"{fore} {last}".strip()
        if len(cand) > len(p["display"]):
            p["display"] = cand
        p["n_papers"] += 1
        p["years"].add(e["year"])
        p["affils"] |= allaffils
        p["has_affil"] |= has
        if washu:
            p["washu"] = True
            if p["earliest_washu"] is None or sd < p["earliest_washu"]:
                p["earliest_washu"] = sd
        if p["earliest"] is None or sd < p["earliest"]:
            p["earliest"] = sd

# ---------------- pass 3: resolve unknowns via OpenAlex author records ----------
AC = HERE / "oa_author_cache"; AC.mkdir(exist_ok=True)
def oa_author(aid):
    slug = aid.rstrip("/").split("/")[-1]
    key = AC / f"{slug}.json"
    if key.exists():
        try:
            return json.loads(key.read_text())
        except Exception:
            pass
    url = f"https://api.openalex.org/authors/{slug}?mailto={MAILTO}"
    for att in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.load(r)
            key.write_text(json.dumps(data)); time.sleep(0.12); return data
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                key.write_text("{}"); return {}
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return {}

unresolved = [p for p in people.values() if not p["has_affil"]]
print(f"resolving {len(unresolved)} unknowns via OpenAlex author records...", file=sys.stderr)
for i, p in enumerate(unresolved):
    hits = set()
    for aid in p["ids"]:
        au = oa_author(aid)
        if not au:
            continue
        cand = []
        for aff in (au.get("affiliations") or []):
            ins = aff.get("institution") or {}
            cand.append((ins.get("display_name") or "", ins.get("ror") or ""))
        for ins in (au.get("last_known_institutions") or []):
            cand.append((ins.get("display_name") or "", ins.get("ror") or ""))
        for dn, ror in cand:
            hits.add(dn)
            if txt_is_washu(dn) or ror == "https://ror.org/01yc7t268":
                p["washu"] = True
                p["author_washu_resolved"] = True
    if hits:
        p["has_affil"] = True
        p["author_affils"] = sorted(hits)
    if i % 30 == 0:
        print(f"  author resolve {i}/{len(unresolved)}", file=sys.stderr)

json.dump({k: {**v, "affils": sorted(v["affils"]), "years": sorted(v["years"]),
               "ids": sorted(v["ids"]),
               "earliest": v["earliest"], "earliest_washu": v["earliest_washu"]}
           for k, v in people.items()},
          open(HERE / "people2.json", "w"), indent=1, default=list)

washu = {k: v for k, v in people.items() if v["washu"]}
unknown = {k: v for k, v in people.items() if not v["has_affil"]}
other = {k: v for k, v in people.items() if v["has_affil"] and not v["washu"]}
print(f"\nnon-team co-authors: {len(people)}", file=sys.stderr)
print(f"  WashU: {len(washu)}", file=sys.stderr)
print(f"  non-WashU (affil known): {len(other)}", file=sys.stderr)
print(f"  affiliation unknown: {len(unknown)}", file=sys.stderr)

def yr(d):
    return str(d[0]) if d and d[0] and d[0] != 9999 else "?"

from datetime import date
L = []
L.append("# Potential members — WashU co-authors not on the team roster\n")
L.append(f"_Generated {date.today().isoformat()} from the {len(entries)} peer-reviewed "
         "papers in `src/data/publications.json` (meeting abstracts and errata excluded)._\n")
L.append("**Method.** For every paper, every co-author was pulled from PubMed and matched "
         "against per-author affiliations from PubMed's `AffiliationInfo` and from OpenAlex's "
         "ROR-resolved institutions. A co-author counts as **WashU** if at least one paper "
         "gives them a Washington University affiliation (or Siteman / Barnes-Jewish / "
         "Mallinckrodt / St. Louis Children's / the McDonnell Genome Institute). People "
         "already on the team roster (current members + alumni) are excluded. Free-text "
         "affiliation blobs were not trusted for the WashU call because pre-2000 records "
         "copy the whole paper's affiliation footnote onto every author.\n")
L.append(f"WashU co-authors: **{len(washu)}**  ·  affiliation not recoverable: **{len(unknown)}**\n")

L.append("## WashU co-authors\n")
L.append("Ordered by the year of their earliest Ley Lab paper.\n")
for k, v in sorted(washu.items(), key=lambda kv: (kv[1]["earliest"] or (9999,), kv[1]["display"].lower())):
    L.append(f"- {yr(v['earliest'])} — {v['display']}")
L.append("")

L.append(f"## Affiliation not recoverable ({len(unknown)})\n")
L.append("Neither PubMed, OpenAlex, nor PMC records a per-author affiliation for these "
         "co-authors on any Ley Lab paper, so WashU status can't be determined "
         "programmatically — the individual author lists would have to be read out of the "
         "papers' PDFs / appendices. This bucket is dominated by two large consortium "
         "papers: *International network of cancer genome projects* (ICGC, Nature 2010; "
         "PMID 20393554) and *Genomic and epigenomic landscapes of adult de novo AML* "
         "(TCGA, NEJM 2013; PMID 23634996). Ordered by earliest paper.\n")
for k, v in sorted(unknown.items(), key=lambda kv: (kv[1]["earliest"] or (9999,), kv[1]["display"].lower())):
    L.append(f"- {yr(v['earliest'])} — {v['display']}  ({v['n_papers']} paper{'s' if v['n_papers'] != 1 else ''})")

out = ROOT / "potential_members.md"
out.write_text("\n".join(L) + "\n")
print(f"wrote {out}", file=sys.stderr)
