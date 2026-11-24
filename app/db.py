"""SQLite storage for the paper library."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "library.db"

JSON_FIELDS = ("authors", "keywords", "tags", "openalex_refs", "contributions", "methodology")

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT UNIQUE NOT NULL,
    path          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    title         TEXT,
    authors       TEXT DEFAULT '[]',
    first_author  TEXT,
    year          INTEGER,
    journal       TEXT,
    doi           TEXT,
    abstract      TEXT,
    keywords      TEXT DEFAULT '[]',
    tags          TEXT DEFAULT '[]',
    notes         TEXT DEFAULT '',
    overview      TEXT,
    overview_model TEXT,
    pages         INTEGER,
    metadata_source TEXT,
    missing       INTEGER DEFAULT 0,
    added_at      TEXT,
    updated_at    TEXT
);

-- One row per entry in a paper's reference list.
CREATE TABLE IF NOT EXISTS citations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    position        INTEGER,
    doi             TEXT,
    title           TEXT,
    author          TEXT,
    year            INTEGER,
    journal         TEXT,
    raw             TEXT,
    source          TEXT,
    cited_paper_id  INTEGER REFERENCES papers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_citations_paper ON citations(paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited_paper_id);

-- Derived: directed "citing cites cited" edges between papers in the library.
CREATE TABLE IF NOT EXISTS citation_links (
    citing_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    cited_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    method     TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id)
);

-- Cached full text, so overviews and citation matching don't re-parse PDFs.
CREATE TABLE IF NOT EXISTS texts (
    paper_id  INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    text      TEXT
);

-- PDFs seen in the Downloads folder. status: 'candidate' (an article), 'other' (not an article),
-- or 'ignored' (the user declined it). Whether a candidate is in the library is checked when read.
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE NOT NULL,
    file_size   INTEGER NOT NULL,
    file_mtime  REAL NOT NULL,
    status      TEXT NOT NULL,
    file_hash   TEXT,
    doi         TEXT,
    title       TEXT,
    author      TEXT,
    family      TEXT,
    year        INTEGER,
    journal     TEXT
);
"""

# Columns added after the first release; created on startup when missing.
MIGRATIONS = {
    "folder": "TEXT DEFAULT ''",
    "file_size": "INTEGER",
    "file_mtime": "REAL",
    "first_author_family": "TEXT",
    "openalex_id": "TEXT",
    "openalex_refs": "TEXT DEFAULT '[]'",
    "cited_by_count": "INTEGER",
    "citations_fetched": "INTEGER DEFAULT 0",
    "contributions": "TEXT DEFAULT '[]'",
    "methodology": "TEXT DEFAULT '[]'",
    "analysis_model": "TEXT",
    "analyzed_at": "TEXT",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    for col, decl in MIGRATIONS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {decl}")
    return conn


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d:
            d[f] = json.loads(d[f] or "[]")
    return d


def list_papers(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM papers ORDER BY year DESC, title").fetchall()
    return [row_to_dict(r) for r in rows]


def get_paper(conn, paper_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return row_to_dict(row) if row else None


def get_by_hash(conn, file_hash: str) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE file_hash = ?", (file_hash,)).fetchone()
    return row_to_dict(row) if row else None


def get_by_path(conn, path: str) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE path = ?", (path,)).fetchone()
    return row_to_dict(row) if row else None


def _encode(fields: dict) -> dict:
    return {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()}


def insert_paper(conn, fields: dict) -> int:
    fields = _encode({**fields, "added_at": now(), "updated_at": now()})
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO papers ({cols}) VALUES ({marks})", tuple(fields.values()))
    conn.commit()
    return cur.lastrowid


def update_paper(conn, paper_id: int, fields: dict) -> None:
    if not fields:
        return
    fields = _encode({**fields, "updated_at": now()})
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE papers SET {sets} WHERE id = ?", (*fields.values(), paper_id))
    conn.commit()


def set_text(conn, paper_id: int, text: str) -> None:
    conn.execute("INSERT OR REPLACE INTO texts (paper_id, text) VALUES (?, ?)", (paper_id, text))
    conn.commit()


def get_text(conn, paper_id: int) -> str | None:
    row = conn.execute("SELECT text FROM texts WHERE paper_id = ?", (paper_id,)).fetchone()
    return row["text"] if row else None


def replace_citations(conn, paper_id: int, refs: list[dict]) -> None:
    conn.execute("DELETE FROM citations WHERE paper_id = ?", (paper_id,))
    conn.executemany(
        "INSERT INTO citations (paper_id, position, doi, title, author, year, journal, raw, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(paper_id, i, r.get("doi"), r.get("title"), r.get("author"), r.get("year"),
          r.get("journal"), r.get("raw"), r.get("source")) for i, r in enumerate(refs)],
    )
    conn.commit()


def list_citations(conn, paper_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM citations WHERE paper_id = ? ORDER BY position", (paper_id,))
    return [dict(r) for r in rows]


def citation_counts(conn) -> dict[int, int]:
    rows = conn.execute("SELECT paper_id, COUNT(*) AS n FROM citations GROUP BY paper_id")
    return {r["paper_id"]: r["n"] for r in rows}


def list_links(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT citing_id, cited_id, method FROM citation_links")]
