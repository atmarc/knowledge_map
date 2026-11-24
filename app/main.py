"""Knowledge Map: a local web table and citation graph of the papers in the sources folder."""

from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyze, backup, citations, citing, config, db, downloads, scanner, works

STATIC = Path(__file__).resolve().parent / "static"

# Large or retired columns that the UI doesn't need
LIST_EXCLUDE = ("openalex_refs", "file_hash", "overview", "overview_model")

app = FastAPI(title="Knowledge Map")


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    # Any web page could otherwise make the browser send POSTs to this local server.
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
    return await call_next(request)


def sources() -> Path | None:
    """The literature folder, or None if it isn't set or doesn't exist."""
    folder = config.path("sources")
    return folder if folder and folder.is_dir() else None


def _scan() -> bool:
    # Scanning a missing folder (e.g. an unmounted drive) would mark every paper as missing.
    folder = sources()
    return bool(folder) and scanner.scan_in_background(folder)


@app.on_event("startup")
def _startup():
    _scan()
    backup.start_scheduler()
    downloads.start_scheduler()
    citing.start_scheduler()


class PaperPatch(BaseModel):
    title: str | None = None
    first_author: str | None = None
    year: int | None = None
    journal: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


def _paper_or_404(conn, paper_id: int) -> dict:
    paper = db.get_paper(conn, paper_id)
    if not paper:
        raise HTTPException(404, "Paper not found")
    return paper


def _with_counts(conn, papers: list[dict]) -> list[dict]:
    """Attach reference counts and in-library citation links to each paper."""
    ref_counts = db.citation_counts(conn)
    cites, cited_by = defaultdict(list), defaultdict(list)
    for link in db.list_links(conn):
        cites[link["citing_id"]].append(link["cited_id"])
        cited_by[link["cited_id"]].append(link["citing_id"])
    for p in papers:
        for k in LIST_EXCLUDE:
            p.pop(k, None)
        p["ref_count"] = ref_counts.get(p["id"], 0)
        p["cites"] = cites[p["id"]]
        p["cited_by"] = cited_by[p["id"]]
    return papers


@app.get("/api/papers")
def papers():
    conn = db.connect()
    return _with_counts(conn, db.list_papers(conn))


@app.get("/api/papers/{paper_id}")
def paper(paper_id: int):
    conn = db.connect()
    return _with_counts(conn, [_paper_or_404(conn, paper_id)])[0]


@app.get("/api/papers/{paper_id}/citations")
def paper_citations(paper_id: int):
    conn = db.connect()
    _paper_or_404(conn, paper_id)
    return db.list_citations(conn, paper_id)


@app.get("/api/graph")
def graph():
    conn = db.connect()
    return {"links": db.list_links(conn)}


@app.get("/api/recommended")
def recommended(limit: int = 200):
    conn = db.connect()
    return {"items": works.recommended(conn, limit=limit), "resolving": works.resolving()}


@app.get("/api/citing")
def citing_works():
    return {"items": citing.items(), **citing.status(), **citing.coverage()}


@app.get("/api/compare")
def compare(ids: str, min_shared: int | None = None):
    paper_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if len(paper_ids) < 2:
        raise HTTPException(400, "Select at least two papers")
    conn = db.connect()
    return {"items": works.compare(conn, paper_ids, min_shared), "resolving": works.resolving()}


@app.get("/api/status")
def status():
    return {
        "sources": str(config.path("sources") or ""),
        "sources_ok": sources() is not None,
        "llm_configured": analyze.is_configured(),
        "llm_model": analyze.model_name(),
        "scan": scanner.status,
        "analysis": analyze.status,
        "backup": backup.status(),
    }


class Settings(BaseModel):
    sources: str | None = None
    downloads: str | None = None
    backup_dir: str | None = None
    backup_hour: int | None = None
    research_field: str | None = None
    claude_model: str | None = None
    openalex_key: str | None = None
    search_fields: list[str] | None = None


@app.get("/api/config")
def get_config():
    return config.all_settings()


@app.put("/api/config")
def put_config(settings: Settings):
    changes = {k: v.strip() if isinstance(v, str) else v for k, v in settings.model_dump(exclude_unset=True).items()}
    for key in ("sources", "downloads"):
        if changes.get(key) and not Path(changes[key]).expanduser().is_dir():
            raise HTTPException(400, f"Folder not found: {changes[key]}")
    if not 0 <= changes.get("backup_hour", 0) <= 23:
        raise HTTPException(400, "The backup hour must be between 0 and 23")
    if "search_fields" in changes:
        changes["search_fields"] = [f for f in config.SEARCH_FIELDS if f in changes["search_fields"]]
        if not changes["search_fields"]:
            raise HTTPException(400, "Choose at least one thing for the search box to look in")
    moved = "sources" in changes and changes["sources"] != config.get("sources")
    saved = config.update(changes)
    if moved:
        _scan()
    if "downloads" in changes:
        downloads.check_in_background()
    return {**saved, "rescanning": moved}


@app.post("/api/backup")
def run_backup():
    return {"started": backup.start(), "backup": backup.status()}


def _downloads_state() -> dict:
    return {"items": downloads.pending(), "checking": downloads.is_checking()}


@app.get("/api/downloads")
def list_downloads():
    return _downloads_state()


@app.post("/api/downloads/check")
def check_downloads():
    downloads.check_in_background()
    return _downloads_state()


@app.post("/api/downloads/{item_id}/add")
def add_download(item_id: int):
    try:
        return downloads.add(item_id, sources())
    except LookupError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))


@app.post("/api/downloads/{item_id}/ignore")
def ignore_download(item_id: int):
    try:
        downloads.ignore(item_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/scan")
def rescan():
    if not sources():
        raise HTTPException(400, "The literature folder isn't set or doesn't exist. Set it in Settings.")
    return {"started": _scan(), "scan": scanner.status}


@app.patch("/api/papers/{paper_id}")
def patch_paper(paper_id: int, patch: PaperPatch):
    conn = db.connect()
    _paper_or_404(conn, paper_id)
    fields = patch.model_dump(exclude_unset=True)
    if "tags" in fields:
        fields["tags"] = sorted({t.strip().lower() for t in fields["tags"] if t.strip()})
    db.update_paper(conn, paper_id, fields)
    return paper(paper_id)


@app.post("/api/papers/{paper_id}/refresh")
def refresh_metadata(paper_id: int):
    conn = db.connect()
    p = _paper_or_404(conn, paper_id)
    path = Path(p["path"])
    bib = scanner.load_bib(sources()) if sources() else {}
    meta = scanner.process_file(path, scanner.bib_entry(bib, path))
    scanner.store(conn, paper_id, meta)
    citations.rebuild_links(conn)
    return paper(paper_id)


def _require_claude() -> None:
    if not analyze.is_configured():
        raise HTTPException(400, "Claude analysis needs the claude CLI (Claude Code), which wasn't found on PATH")


@app.post("/api/papers/{paper_id}/analyze")
def analyze_paper(paper_id: int):
    _require_claude()
    conn = db.connect()
    _paper_or_404(conn, paper_id)
    try:
        analyze.analyze_one(paper_id)
    except Exception as e:
        raise HTTPException(502, f"Analysis failed: {e}")
    return paper(paper_id)


class AnalyzeRequest(BaseModel):
    ids: list[int] | None = None  # default: every paper not analyzed yet


@app.post("/api/analyze")
def analyze_many(req: AnalyzeRequest):
    _require_claude()
    conn = db.connect()
    ids = req.ids
    if ids is None:
        ids = [r["id"] for r in conn.execute(
            "SELECT p.id FROM papers p JOIN texts t ON t.paper_id = p.id"
            " WHERE p.missing = 0 AND p.analyzed_at IS NULL AND length(t.text) >= 500 ORDER BY p.id")]
    started = analyze.analyze_in_background(ids)
    return {"started": started, "count": len(ids), "analysis": analyze.status}


@app.get("/api/papers/{paper_id}/pdf")
def open_pdf(paper_id: int):
    p = _paper_or_404(db.connect(), paper_id)
    path = Path(p["path"])
    if not path.exists():
        raise HTTPException(404, "File no longer exists")
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}"})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
