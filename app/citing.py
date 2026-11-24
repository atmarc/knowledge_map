"""Works outside the library that cite many library papers, from OpenAlex's cited-by lists.

Refreshed once a day. One paged query asks for every work citing any of a group of library papers
(numbered pages: cursor paging needs an API key), returning only each work's ID and reference list.
The library papers it cites are counted here, and details are then fetched only for works citing at
least two of them. A full refresh takes about 120 requests; without an OpenAlex API key (Settings),
OpenAlex allows ~1000 a day shared by everyone on this IP address.
Papers OpenAlex doesn't know (no openalex_id) can't be screened.
"""

import json
import threading
import time
from datetime import datetime, timedelta

from . import db, extract

GROUP = 50            # library papers per query (OpenAlex allows up to 100 OR'd values)
PER_PAGE = 200
MAX_RESULTS = 10_000  # without an API key OpenAlex serves only numbered pages, up to this many results
DETAIL_BATCH = 50
MIN_CITES = 2
MAX_WORKS = 600       # works whose details are fetched and kept
SKIP_TYPES = {"erratum", "paratext", "retraction", "peer-review"}
STATE_PATH = db.DB_PATH.parent / "citing.json"
CHECK_EVERY_S = 1800
RETRY_AFTER = timedelta(hours=3)
FIRST_CHECK_S = 60    # let the startup scan and the network settle first

_lock = threading.Lock()
_running = False


def _ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS citing_works (
        openalex_id TEXT PRIMARY KEY, doi TEXT, title TEXT, authors TEXT, year INTEGER, journal TEXT,
        cited_by_count INTEGER, cites TEXT)""")


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def _short(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _page(params: dict) -> dict:
    r = extract._get(extract.OPENALEX_URL, params=params)
    if r is None or r.status_code == 429:
        raise RuntimeError("OpenAlex is unreachable or its daily request budget is used up")
    if r.status_code != 200:
        raise RuntimeError(f"OpenAlex answered {r.status_code}: {r.text[:200]}")
    return r.json()


def _groups(counts: dict[str, int]) -> list[list[str]]:
    """Library works batched so that each query's results fit in the pages reachable without a key."""
    groups, size = [[]], 0
    for oa in sorted(counts, key=lambda oa: -counts[oa]):
        n = counts[oa] or 0
        if groups[-1] and (len(groups[-1]) >= GROUP or size + n > MAX_RESULTS):
            groups.append([])
            size = 0
        groups[-1].append(oa)
        size += n
    return [g for g in groups if g]


def _citing_refs(counts: dict[str, int]) -> dict[str, set[str]]:
    """Citing work -> the library works (OpenAlex IDs) it cites. `counts`: library work -> cited-by count."""
    library_ids, out = set(counts), {}
    for group in _groups(counts):
        for page in range(1, MAX_RESULTS // PER_PAGE + 1):
            d = _page({"filter": "cites:" + "|".join(group), "select": "id,referenced_works",
                       "per-page": PER_PAGE, "page": page})
            for w in d["results"]:
                hits = {_short(u) for u in w.get("referenced_works") or []} & library_ids
                if hits:
                    out.setdefault(_short(w["id"]), set()).update(hits)
            if page * PER_PAGE >= d["meta"]["count"]:
                break
    return out


def _details(work_ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(work_ids), DETAIL_BATCH):
        d = _page({"filter": "ids.openalex:" + "|".join(work_ids[i:i + DETAIL_BATCH]), "per-page": DETAIL_BATCH,
                   "select": "id,doi,display_name,publication_year,authorships,primary_location,cited_by_count,type"})
        out += d["results"]
    return out


def refresh() -> str:
    conn = db.connect()
    _ensure_table(conn)
    papers = conn.execute("SELECT id, openalex_id, doi, title, cited_by_count FROM papers WHERE missing = 0").fetchall()
    by_openalex, counts = {}, {}
    for p in papers:
        if p["openalex_id"]:
            by_openalex.setdefault(p["openalex_id"], []).append(p["id"])
            counts[p["openalex_id"]] = max(counts.get(p["openalex_id"], 0), p["cited_by_count"] or 0)
    lib_dois = {p["doi"].lower() for p in papers if p["doi"]}
    lib_titles = {extract.norm(p["title"]) for p in papers if p["title"]}

    refs = _citing_refs(counts)
    ranked = sorted((w for w, hits in refs.items() if len(hits) >= MIN_CITES and w not in by_openalex),
                    key=lambda w: -len(refs[w]))[:MAX_WORKS]

    works = {}  # by normalized title: a preprint and its published version count once
    for w in _details(ranked):
        doi = (w.get("doi") or "").removeprefix("https://doi.org/").lower() or None
        title = w.get("display_name") or ""
        key = extract.norm(title) or _short(w["id"])
        if w.get("type") in SKIP_TYPES or doi in lib_dois or key in lib_titles:
            continue
        cites = {pid for oa in refs[_short(w["id"])] for pid in by_openalex[oa]}
        item = {
            "openalex_id": _short(w["id"]), "doi": doi, "title": title,
            "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])[:3] if a.get("author")],
            "year": w.get("publication_year"),
            "journal": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            "cited_by_count": w.get("cited_by_count"), "cites": cites,
        }
        if key in works:  # keep the version with a journal, merge what they cite
            item["cites"] |= works[key]["cites"]
            if works[key]["journal"] and not item["journal"]:
                item = {**works[key], "cites": item["cites"]}
        works[key] = item

    with conn:
        conn.execute("DELETE FROM citing_works")
        conn.executemany(
            "INSERT INTO citing_works VALUES (:openalex_id, :doi, :title, :authors, :year, :journal, :cited_by_count, :cites)",
            [{**w, "authors": json.dumps(w["authors"]), "cites": json.dumps(sorted(w["cites"]))} for w in works.values()])
    return f"{len(works)} works found citing 2+ of the {len(by_openalex)} library papers known to OpenAlex"


def items() -> list[dict]:
    conn = db.connect()
    _ensure_table(conn)
    rows = conn.execute("SELECT * FROM citing_works").fetchall()
    out = [{**dict(r), "authors": json.loads(r["authors"]), "cites": json.loads(r["cites"])} for r in rows]
    return sorted(out, key=lambda w: (-len(w["cites"]), -(w["cited_by_count"] or 0)))


def coverage() -> dict:
    """How many library papers OpenAlex knows, i.e. can be screened for citing works."""
    row = db.connect().execute("SELECT count(openalex_id), count(*) FROM papers WHERE missing = 0").fetchone()
    return {"screened": row[0], "papers": row[1]}


def status() -> dict:
    state = _load_state()
    return {"running": _running, "last_success": state.get("last_success"), "last_result": state.get("last_result")}


def _run() -> None:
    global _running
    now = datetime.now().isoformat(timespec="seconds")
    result = {"ok": False, "at": now}
    try:
        result["message"], result["ok"] = refresh(), True
    except Exception as e:  # keep yesterday's ranking
        result["message"] = str(e)[:300]
    finally:
        state = _load_state()
        state["last_result"] = result
        if result["ok"]:
            state["last_success"] = now
        _save_state(state)
        _running = False


def start() -> bool:
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    threading.Thread(target=_run, daemon=True).start()
    return True


def daily_tick(now: datetime | None = None) -> bool:
    """Refresh once per calendar day; after a failure, try again a few hours later."""
    now = now or datetime.now()
    state = _load_state()
    if (state.get("last_success") or "").startswith(now.date().isoformat()):
        return False
    last = (state.get("last_result") or {}).get("at")
    if last and now - datetime.fromisoformat(last) < RETRY_AFTER:
        return False
    return start()


def start_scheduler() -> None:
    def loop():
        time.sleep(FIRST_CHECK_S)
        while True:
            daily_tick()
            time.sleep(CHECK_EVERY_S)
    threading.Thread(target=loop, daemon=True).start()
