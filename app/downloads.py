"""Spot article PDFs in the Downloads folder and move them into the library on request.

A PDF counts as an article only if it carries a DOI that Crossref confirms belongs to it. Downloads
also holds private documents, so only DOIs printed in a PDF are ever sent out, and a PDF without
one causes no network traffic at all.
"""

import itertools
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from . import citations, config, db, extract, scanner

ARTICLE_TYPES = {"journal-article", "proceedings-article", "posted-content"}
CHECK_EVERY_S = 900
MIN_AGE_S = 30  # a younger file may still be being written
MIN_TITLE_MATCH = 30  # shortest title that may match a library title by prefix alone
MAX_TITLE_BYTES = 150
MAX_BODY_DOIS = 6  # lookups per PDF beyond its first two pages; a bibliography can hold hundreds

_lock = threading.Lock()
_running = False


def _lookup(doi: str) -> dict | None:
    """Crossref record for a DOI, None if it has none. Raises when Crossref can't be asked,
    so that an outage doesn't get files remembered as not being articles."""
    r = extract._get(f"{extract.CROSSREF_URL}/{doi}", serial=True)
    if r is None:
        raise ConnectionError("Crossref is unreachable")
    try:
        return r.json()["message"] if r.status_code == 200 else None
    except ValueError:
        return None


def _ranked_dois(text: str) -> list[str]:
    """DOIs in a whole PDF, likeliest to be its own first: some journals (Science) print a paper's
    DOI only at the end, and it tends to repeat or follow the references, where each citation appears once."""
    found = [extract._clean_doi(m.group(1)) for m in extract.DOI_RE.finditer(text)]
    counts, last = Counter(found), {doi: i for i, doi in enumerate(found)}
    return sorted(counts, key=lambda doi: (-counts[doi], -last[doi]))


def _classify(path: Path) -> dict:
    try:
        meta = {k: v for k, v in (PdfReader(path).metadata or {}).items()}
        page1, head = extract.read_text(path, max_pages=1), extract.read_text(path, max_pages=2)
        full = extract.read_text(path)
    except Exception:  # encrypted or damaged
        return {"status": "other"}
    near = extract.doi_candidates(meta, head)
    doi, work = extract.verified_work(near, page1, head, lookup=_lookup)
    if not work:
        tried = {d for d, _ in near}
        doi, work = extract.verified_work([(d, False) for d in _ranked_dois(full) if d not in tried][:MAX_BODY_DOIS],
                                          page1, head, lookup=_lookup)
    if not work or work.get("type") not in ARTICLE_TYPES:
        return {"status": "other"}
    cr, _ = extract.parse_crossref(work)
    if not (cr["title"] and cr["first_author_family"] and cr["year"]):
        return {"status": "other"}  # couldn't be named by the library's convention
    author = cr["authors"][0] + (" et al." if len(cr["authors"]) > 1 else "")
    return {"status": "candidate", "file_hash": extract.file_hash(path), "doi": doi, "title": cr["title"],
            "author": author, "family": cr["first_author_family"], "year": cr["year"], "journal": cr["journal"]}


def check() -> None:
    """Classify the PDFs in Downloads that are new or changed, and forget those that are gone."""
    conn = db.connect()
    known = {r["path"]: r for r in conn.execute("SELECT path, file_size, file_mtime FROM downloads")}
    folder = config.path("downloads")  # None: watching is switched off
    pdfs = [p for p in folder.iterdir() if p.suffix.lower() == ".pdf" and p.is_file()] if folder and folder.is_dir() else []
    for gone in known.keys() - {str(p) for p in pdfs}:
        conn.execute("DELETE FROM downloads WHERE path = ?", (gone,))
    conn.commit()

    for path in pdfs:
        st, row = path.stat(), known.get(str(path))
        if row and (row["file_size"], row["file_mtime"]) == (st.st_size, st.st_mtime):
            continue
        if st.st_size == 0 or time.time() - st.st_mtime < MIN_AGE_S:
            continue
        try:
            fields = {"path": str(path), "file_size": st.st_size, "file_mtime": st.st_mtime, **_classify(path)}
        except ConnectionError:
            return  # try the rest next time
        conn.execute("DELETE FROM downloads WHERE path = ?", (str(path),))
        conn.execute(f"INSERT INTO downloads ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     tuple(fields.values()))
        conn.commit()


def _library(conn) -> tuple[set, set, list]:
    rows = conn.execute("SELECT file_hash, doi, title FROM papers WHERE missing = 0").fetchall()
    return ({r["file_hash"] for r in rows}, {r["doi"].lower() for r in rows if r["doi"]},
            [t for t in (extract.norm(r["title"]) for r in rows) if t])


def _in_library(item, library: tuple[set, set, list]) -> bool:
    hashes, dois, titles = library
    if item["file_hash"] in hashes or item["doi"].lower() in dois:
        return True
    # By title too: the library may hold a preprint under another DOI, or a title cut off by a
    # filename ("Ryan2020 - Full-Field Modeling of Heat Transfer in Asteroid Regolith 1 ...").
    title = extract.norm(item["title"])
    return any(t == title or (min(len(t), len(title)) >= MIN_TITLE_MATCH and (t.startswith(title) or title.startswith(t)))
               for t in titles)


def library_filename(family: str, year: int, title: str) -> str:
    """The library's naming convention, without the extension: AuthorYear-Article name."""
    author = re.sub(r"[^\w'-]|\d|_", "", family)
    safe = re.sub(r"\s+", " ", re.sub(r'[/\\:*?"<>|\x00-\x1f]', " ", title)).strip()
    cut = safe.encode()[:MAX_TITLE_BYTES].decode(errors="ignore")
    if cut != safe and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return f"{author}{year}-{cut.rstrip(' .,;-')}"


def pending() -> list[dict]:
    """Articles waiting for the user's decision, newest first; several copies of one count once."""
    conn = db.connect()
    declined = {r["doi"] for r in conn.execute("SELECT doi FROM downloads WHERE status = 'ignored'")}
    library, seen, items = _library(conn), set(), []
    for r in conn.execute("SELECT * FROM downloads WHERE status = 'candidate' ORDER BY file_mtime DESC"):
        if r["doi"] in declined or r["doi"] in seen or _in_library(r, library) or not Path(r["path"]).exists():
            continue
        seen.add(r["doi"])
        items.append({"id": r["id"], "title": r["title"], "author": r["author"], "year": r["year"], "journal": r["journal"],
                      "filename": Path(r["path"]).name,
                      "new_filename": library_filename(r["family"], r["year"], r["title"]) + ".pdf"})
    return items


def _move(src: Path, sources: Path, stem: str) -> Path:
    # Under a hidden name until complete, so a running scan can't pick up half a file. Linking
    # into place, unlike renaming, fails rather than overwrites if the name is taken.
    part = sources / f".km-incoming-{os.getpid()}-{threading.get_ident()}.part"
    try:
        shutil.copyfile(src, part)
        if part.stat().st_size != src.stat().st_size:
            raise OSError(f"incomplete copy of {src.name}")
        for n in itertools.count(1):
            dst = sources / (f"{stem}.pdf" if n == 1 else f"{stem} ({n}).pdf")
            try:
                os.link(part, dst)
                break
            except FileExistsError:
                continue
    finally:
        part.unlink(missing_ok=True)
    src.unlink()
    return dst


def _candidate(conn, item_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM downloads WHERE id = ? AND status = 'candidate'", (item_id,)).fetchone()
    if not row or not Path(row["path"]).exists():
        raise LookupError("That file is no longer in Downloads")
    return row


def add(item_id: int, sources: Path) -> dict:
    """Move a candidate into the library under its conventional name and index it."""
    if not sources or not sources.is_dir():
        raise LookupError("The literature folder isn't set or doesn't exist (see Settings)")
    conn = db.connect()
    row = _candidate(conn, item_id)
    if _in_library(row, _library(conn)):
        raise FileExistsError("This paper is already in your library")
    dst = _move(Path(row["path"]), sources, library_filename(row["family"], row["year"], row["title"]))
    conn.execute("DELETE FROM downloads WHERE id = ?", (item_id,))
    conn.commit()

    fields, file_hash = scanner._file_fields(dst, sources), extract.file_hash(dst)
    earlier = db.get_by_hash(conn, file_hash)  # a paper whose file went missing keeps its tags and notes
    if earlier:
        db.update_paper(conn, earlier["id"], fields)
        paper_id = earlier["id"]
    else:
        paper_id = db.insert_paper(conn, {"file_hash": file_hash, "title": dst.stem, **fields})
    scanner.store(conn, paper_id, scanner.process_file(dst, None))
    citations.rebuild_links(conn)
    return {"paper_id": paper_id, "filename": dst.name}


def ignore(item_id: int) -> None:
    """Never ask about this paper again, in any copy, while it stays in Downloads."""
    conn = db.connect()
    row = _candidate(conn, item_id)
    conn.execute("UPDATE downloads SET status = 'ignored' WHERE doi = ?", (row["doi"],))
    conn.commit()


def is_checking() -> bool:
    return _running


def check_in_background() -> bool:
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def run():
        global _running
        try:
            check()
        except Exception as e:  # a bad pass must not stop later ones
            print(f"Downloads check failed: {e}", flush=True)
        finally:
            _running = False

    threading.Thread(target=run, daemon=True).start()
    return True


def start_scheduler() -> None:
    def loop():
        while True:
            check_in_background()
            time.sleep(CHECK_EVERY_S)
    threading.Thread(target=loop, daemon=True).start()
