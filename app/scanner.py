"""Background scan of the sources folder: new PDFs, moves, metadata, references, links."""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import citations, db, extract

WORKERS = 4
RELINK_EVERY = 25

_lock = threading.Lock()
status = {"running": False, "total": 0, "done": 0, "current": None, "last": None, "errors": 0}


def load_bib(sources: Path) -> dict[str, dict]:
    """BibTeX entries keyed by normalized PDF filename (see bib_entry)."""
    entries = {}
    for bib in sorted(sources.glob("*.bib")):
        try:
            entries.update({extract.norm(k): v for k, v in extract.parse_bib(bib).items()})
        except Exception:
            pass
    return entries


def bib_entry(bib: dict, path: Path) -> dict | None:
    return bib.get(extract.norm(path.name))


def process_file(path: Path, bib: dict | None) -> dict:
    """Network + parsing work for one PDF; safe to run in a worker thread."""
    try:
        meta = extract.extract_metadata(path, bib=bib)
    except Exception as e:
        meta = {"title": path.stem, "metadata_source": f"error: {e}", "_refs": []}
    try:
        meta["_text"] = extract.read_text(path)
    except Exception:
        meta["_text"] = ""
    return meta


def store(conn, paper_id: int, meta: dict) -> None:
    meta = dict(meta)
    refs, text = meta.pop("_refs", []), meta.pop("_text", None)
    db.update_paper(conn, paper_id, {**meta, "citations_fetched": 1})
    db.replace_citations(conn, paper_id, refs)
    if text is not None:
        db.set_text(conn, paper_id, text)


def _file_fields(path: Path, sources: Path) -> dict:
    st = path.stat()
    folder = str(path.parent.relative_to(sources)) if path.parent != sources else ""
    return {"path": str(path), "filename": path.name, "folder": folder,
            "file_size": st.st_size, "file_mtime": st.st_mtime, "missing": 0}


def scan(sources: Path) -> dict:
    with _lock:
        if status["running"]:
            return status
        status.update(running=True, total=0, done=0, current="listing files", errors=0, last=None)
    try:
        return _scan(sources)
    finally:
        status.update(running=False, current=None)


def _scan(sources: Path) -> dict:
    conn = db.connect()
    bib = load_bib(sources)
    seen, todo, new_ids, moved = set(), [], [], 0

    # Phase 1: cheap bookkeeping (stat first, hash only when something changed)
    for pdf in sorted(p for p in sources.rglob("*.pdf") if not any(x.startswith(".") for x in p.parts)):
        fields = _file_fields(pdf, sources)
        row = db.get_by_path(conn, str(pdf))
        if row and row["file_size"] == fields["file_size"] and row["file_mtime"] == fields["file_mtime"]:
            seen.add(row["id"])
        else:
            h = extract.file_hash(pdf)
            row = db.get_by_hash(conn, h)
            if row and row["path"] != str(pdf) and Path(row["path"]).exists():
                continue  # identical copy of a PDF that is already in the library
            if row:
                if row["path"] != str(pdf):
                    moved += 1
                db.update_paper(conn, row["id"], fields)
            else:
                pid = db.insert_paper(conn, {"file_hash": h, "title": pdf.stem, **fields})
                row = db.get_paper(conn, pid)
                new_ids.append(pid)
            seen.add(row["id"])
        if not row["citations_fetched"]:
            todo.append((row["id"], pdf))

    missing = 0
    for p in db.list_papers(conn):
        if p["id"] not in seen and not p["missing"]:
            db.update_paper(conn, p["id"], {"missing": 1})
            missing += 1

    # Phase 2: metadata, references and text in parallel; writes stay on this thread
    status.update(total=len(todo), current=None)
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(process_file, pdf, bib_entry(bib, pdf)): (pid, pdf) for pid, pdf in todo}
        for fut in as_completed(futures):
            pid, pdf = futures[fut]
            meta = fut.result()
            if str(meta.get("metadata_source", "")).startswith("error"):
                status["errors"] += 1
            store(conn, pid, meta)
            status["done"] += 1
            status["current"] = pdf.name
            if status["done"] % RELINK_EVERY == 0:
                citations.rebuild_links(conn)

    links = citations.rebuild_links(conn)
    result = {"added": len(new_ids), "new_ids": new_ids, "moved": moved, "missing": missing, "processed": len(todo), "links": links}
    status["last"] = result
    return result


def scan_in_background(sources: Path) -> bool:
    if status["running"]:
        return False
    threading.Thread(target=scan, args=(sources,), daemon=True).start()
    return True
