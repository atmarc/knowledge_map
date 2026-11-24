"""Cited works across the library: recommendations and shared references.

Every reference is reduced to a key identifying the cited work:
  lib:<id>     a paper in the library (from citation_links, so OpenAlex/PDF matches count too)
  doi:<doi>    an external work with a DOI
  t:<title>    an external work known only by its (normalized) title
References with neither DOI nor title (e.g. AAS-style "Paige et al. 2010, Sci, 330, 479") are
attributed to a known work when its title appears in the raw text, and skipped otherwise.

Crossref reference entries often carry only a DOI, so titles/authors for displayed works are
looked up on Crossref in the background and cached in the `works` table.
"""

import json
import threading
from collections import defaultdict

from . import db, extract

MIN_TITLE = 20          # normalized chars for a title to be usable as a key
RESOLVE_BATCH = 200     # DOIs to look up per request at most

_resolver = {"running": False, "queue": set()}
_lock = threading.Lock()


def _ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS works (
        doi TEXT PRIMARY KEY, title TEXT, authors TEXT, year INTEGER, journal TEXT, found INTEGER)""")


def _cached(conn, dois) -> dict[str, dict]:
    _ensure_table(conn)
    out = {}
    dois = list(dois)
    for i in range(0, len(dois), 500):
        chunk = dois[i:i + 500]
        q = f"SELECT * FROM works WHERE doi IN ({','.join('?' * len(chunk))})"
        for r in conn.execute(q, chunk):
            out[r["doi"]] = {**dict(r), "authors": json.loads(r["authors"] or "[]")}
    return out


def _collect(conn):
    """key -> set of citing paper ids, and key -> list of reference rows (for display)."""
    citing = defaultdict(set)
    entries = defaultdict(list)
    for l in db.list_links(conn):
        citing[f"lib:{l['cited_id']}"].add(l["citing_id"])

    rows = [dict(r) for r in conn.execute(
        "SELECT paper_id, doi, title, author, year, journal, raw FROM citations WHERE cited_paper_id IS NULL")]
    cache = _cached(conn, {r["doi"] for r in rows if r["doi"]})

    # Titles that belong to a DOI, so title-only references to the same work merge with it.
    title_to_doi = {}
    for r in rows:
        title = r["title"] or (cache.get(r["doi"]) or {}).get("title")
        if r["doi"] and len(extract.norm(title)) >= MIN_TITLE:
            title_to_doi.setdefault(extract.norm(title), r["doi"])

    raw_only = []
    for r in rows:
        nt = extract.norm(r["title"])
        if r["doi"]:
            key = f"doi:{r['doi']}"
        elif len(nt) >= MIN_TITLE:
            key = f"doi:{title_to_doi[nt]}" if nt in title_to_doi else f"t:{nt[:120]}"
        else:
            raw_only.append(r)
            continue
        citing[key].add(r["paper_id"])
        entries[key].append(r)

    # Raw-only references: attribute to a work cited at least twice whose title appears in the text.
    known = [(t, f"doi:{d}") for t, d in title_to_doi.items() if len(citing[f"doi:{d}"]) >= 2]
    known += [(k[2:], k) for k in list(citing) if k.startswith("t:") and len(citing[k]) >= 2]
    for r in raw_only:
        text = extract.norm(r["raw"])
        if not text:
            continue
        for t, key in known:
            if t in text:
                citing[key].add(r["paper_id"])
                entries[key].append(r)
                break
    return citing, entries, cache


def _describe(conn, key: str, rows: list[dict], cache: dict) -> dict:
    if key.startswith("lib:"):
        p = db.get_paper(conn, int(key[4:]))
        return {"key": key, "paper_id": p["id"], "title": p["title"] or p["filename"], "authors": p["authors"],
                "year": p["year"], "journal": p["journal"], "doi": p["doi"]}
    doi = key[4:] if key.startswith("doi:") else None
    c = cache.get(doi) or {}
    pick = lambda f: next((r[f] for r in rows if r.get(f)), None)
    title = c.get("title") or pick("title")
    author = (c.get("authors") or [None])[0] or pick("author")
    return {
        "key": key, "paper_id": None, "doi": doi,
        "title": title,
        "authors": c.get("authors") or ([author] if author else []),
        "year": c.get("year") or pick("year"),
        "journal": c.get("journal") or pick("journal"),
        "raw": None if title else pick("raw"),
        "pending": bool(doi) and doi not in cache,
    }


def _queue_lookups(items: list[dict]) -> None:
    dois = [it["doi"] for it in items if it.get("pending")][:RESOLVE_BATCH]
    if not dois:
        return
    with _lock:
        _resolver["queue"].update(dois)
        if _resolver["running"]:
            return
        _resolver["running"] = True
    threading.Thread(target=_resolve_loop, daemon=True).start()


def _resolve_loop() -> None:
    conn = db.connect()
    _ensure_table(conn)
    while True:
        with _lock:
            if not _resolver["queue"]:
                _resolver["running"] = False
                return
            doi = _resolver["queue"].pop()
        msg = extract.crossref_work(doi)
        meta = extract.parse_crossref(msg)[0] if msg else {}
        conn.execute("INSERT OR REPLACE INTO works (doi, title, authors, year, journal, found) VALUES (?, ?, ?, ?, ?, ?)",
                     (doi, meta.get("title"), json.dumps(meta.get("authors") or []), meta.get("year"),
                      meta.get("journal"), int(bool(msg))))
        conn.commit()


def resolving() -> int:
    return len(_resolver["queue"]) + int(_resolver["running"])


def recommended(conn, limit: int = 200, min_citing: int = 2) -> list[dict]:
    """External works cited by the most library papers."""
    citing, entries, cache = _collect(conn)
    ranked = sorted((k for k in citing if not k.startswith("lib:") and len(citing[k]) >= min_citing),
                    key=lambda k: -len(citing[k]))[:limit]
    items = [{**_describe(conn, k, entries[k], cache), "cited_by": sorted(citing[k])} for k in ranked]
    _queue_lookups(items)
    return items


def compare(conn, ids: list[int], min_shared: int | None = None) -> list[dict]:
    """Works cited by at least `min_shared` (default: all) of the given papers."""
    citing, entries, cache = _collect(conn)
    selected = set(ids)
    need = min_shared or len(selected)
    items = []
    for k, who in citing.items():
        shared = who & selected
        if len(shared) >= need:
            items.append({**_describe(conn, k, entries[k], cache), "cited_by": sorted(shared),
                          "library_citations": len(who)})
    items.sort(key=lambda it: (-len(it["cited_by"]), -it["library_citations"], str(it["title"] or "")))
    _queue_lookups(items)
    return items
