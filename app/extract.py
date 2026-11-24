"""Pull bibliographic metadata and reference lists out of a PDF.

Metadata, best source first:
1. The user's BibTeX file (matched by PDF filename) — curated, trusted.
2. A DOI (BibTeX, PDF metadata, or first pages, verified against the title)
   looked up on Crossref.
3. The PDF's embedded metadata, then the filename ("Ryan2020 - Title.pdf").
If no DOI is found, Crossref is searched by title and accepted only on a
near-exact title match.

References come from Crossref's deposited reference list; OpenAlex adds its
own list of referenced works plus a global cited-by count.
"""

import hashlib
import html
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import httpx
from pypdf import PdfReader

from . import config

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.IGNORECASE)
YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"
HEADERS = {"User-Agent": "knowledge-map/0.1 (local paper library)"}

MAX_TEXT_CHARS = 2_000_000

_http = httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)
_crossref_lock = threading.Lock()  # Crossref's public pool allows one concurrent request


def _get(url: str, params: dict | None = None, serial: bool = False) -> httpx.Response | None:
    """GET with retries on rate limiting / server errors; returns None if it keeps failing."""
    # Without a key, OpenAlex shares one small daily budget (~1000 requests) among everyone on this IP address.
    key = config.get("openalex_key")
    if key and url.startswith(OPENALEX_URL):
        params = {**(params or {}), "api_key": key}
    for attempt in range(4):
        try:
            if serial:
                with _crossref_lock:
                    r = _http.get(url, params=params)
            else:
                r = _http.get(url, params=params)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            delay = float(r.headers.get("retry-after") or 2 ** attempt)
        except httpx.HTTPError:
            delay = 2 ** attempt
        time.sleep(min(delay, 30))
    return None


def norm(s: str | None) -> str:
    """Lowercase ASCII letters and digits only: robust to accents, hyphenation and line breaks."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", html.unescape(s))
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path, max_pages: int | None = None) -> str:
    if shutil.which("pdftotext"):
        cmd = ["pdftotext", "-enc", "UTF-8"]
        if max_pages:
            cmd += ["-l", str(max_pages)]
        try:
            out = subprocess.run([*cmd, str(path), "-"], capture_output=True, text=True, timeout=180)
            if out.stdout.strip():
                return out.stdout[:MAX_TEXT_CHARS]
        except subprocess.TimeoutExpired:
            pass
    reader = PdfReader(path)
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    parts = []
    for p in pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)[:MAX_TEXT_CHARS]


# ---------- BibTeX ----------

def _bib_fields(body: str) -> dict:
    fields, i = {}, 0
    for m in re.finditer(r"(\w+)\s*=\s*", body):
        if m.start() < i:
            continue
        key, j = m.group(1).lower(), m.end()
        if j < len(body) and body[j] == "{":
            depth, k = 0, j
            while k < len(body):
                depth += {"{": 1, "}": -1}.get(body[k], 0)
                if depth == 0:
                    break
                k += 1
            fields[key] = body[j + 1:k]
            i = k + 1
        else:
            v = re.match(r'"([^"]*)"|([^,\n]+)', body[j:])
            fields[key] = (v.group(1) or v.group(2) or "").strip() if v else ""
            i = j + (v.end() if v else 0)
    return {k: re.sub(r"[{}]", "", re.sub(r"\s+", " ", v)).strip() for k, v in fields.items()}


def parse_bib(path: Path) -> dict[str, dict]:
    """Map PDF basename -> entry for every BibTeX entry that has a `file` field."""
    entries = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for chunk in re.split(r"\n(?=@)", text):
        if not re.match(r"@(?!comment)\w+\s*\{", chunk, re.IGNORECASE):
            continue
        f = _bib_fields(chunk[chunk.index("{") + 1:])
        m = re.search(r"([^/\\:]+\.pdf)", f.get("file", ""), re.IGNORECASE)
        if not m:
            continue
        authors = []
        for a in re.split(r"\s+and\s+", f.get("author", "")):
            if "," in a:
                family, given = (x.strip() for x in a.split(",", 1))
                authors.append((f"{given} {family}".strip(), family))
            elif a.strip():
                authors.append((a.strip(), a.split()[-1]))
        year = YEAR_RE.search(f.get("year", ""))
        entries[m.group(1)] = {
            "doi": f.get("doi") or None,
            "title": f.get("title") or None,
            "authors": [a[0] for a in authors],
            "first_author_family": authors[0][1] if authors else None,
            "year": int(year.group(1)) if year else None,
            "journal": f.get("journal") or f.get("booktitle") or None,
        }
    return entries


# ---------- Crossref / OpenAlex ----------

def _int_year(v) -> int | None:
    m = YEAR_RE.search(str(v or ""))
    return int(m.group(1)) if m else None


def crossref_work(doi: str) -> dict | None:
    r = _get(f"{CROSSREF_URL}/{doi}", serial=True)
    try:
        return r.json()["message"] if r is not None and r.status_code == 200 else None
    except ValueError:
        return None


def crossref_search(title: str) -> str | None:
    """Find a DOI by title; only accept a near-identical title."""
    r = _get(CROSSREF_URL, params={"query.bibliographic": title, "rows": 3, "select": "DOI,title"}, serial=True)
    try:
        items = r.json()["message"]["items"] if r is not None and r.status_code == 200 else []
    except ValueError:
        return None
    want = norm(title)
    for it in items:
        got = norm((it.get("title") or [""])[0])
        if got and (got == want or (min(len(got), len(want)) / max(len(got), len(want)) > 0.9
                                    and (got in want or want in got))):
            return it["DOI"]
    return None


def openalex_work(doi: str) -> dict | None:
    r = _get(f"{OPENALEX_URL}/doi:{doi}", params={"select": "id,referenced_works,cited_by_count"})
    try:
        if r is None or r.status_code != 200:
            return None
        d = r.json()
    except ValueError:
        return None
    short = lambda u: u.rsplit("/", 1)[-1]
    return {
        "openalex_id": short(d["id"]),
        "openalex_refs": [short(u) for u in d.get("referenced_works", [])],
        "cited_by_count": d.get("cited_by_count"),
    }


def parse_crossref(msg: dict) -> tuple[dict, list[dict]]:
    authors, family = [], None
    for a in msg.get("author", []):
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
        if name:
            authors.append(name)
            family = family or a.get("family") or name.split()[-1]

    year = None
    for key in ("published-print", "published-online", "issued", "published"):
        parts = msg.get(key, {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = parts[0][0]
            break

    abstract = msg.get("abstract")
    if abstract:
        abstract = html.unescape(re.sub(r"<[^>]+>", " ", abstract))
        abstract = re.sub(r"\s+", " ", abstract).strip()
        abstract = re.sub(r"^Abstract\s*", "", abstract, flags=re.IGNORECASE)

    def clean(s):  # Crossref titles carry HTML entities and markup like <i>, <sub>
        if not s:
            return s
        # "H <sub>2</sub> O" -> "H2O", but keep the space in "M<sup>3</sup> on"
        s = re.sub(r"\s*<(sub|sup|inf)>\s*", "", s)
        s = re.sub(r"\s*</(sub|sup|inf)>(\s+(?=[A-Z][a-z]?\b))?", "", s)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()

    meta = {
        "title": clean((msg.get("title") or [None])[0]),
        "authors": authors,
        "first_author_family": family,
        "year": year,
        "journal": clean((msg.get("container-title") or [None])[0]),
        "abstract": abstract,
        "keywords": msg.get("subject", []),
        "cited_by_count": msg.get("is-referenced-by-count"),
    }

    refs = []
    for r in msg.get("reference", []):
        raw = r.get("unstructured")
        author = r.get("author")
        year = _int_year(r.get("year"))
        if raw and not author:
            m = re.match(r"\s*([A-ZÀ-Þ][\w'’\-À-ÿ]+)", raw)
            author = m.group(1) if m else None
        if raw and not year:
            year = _int_year(raw)
        refs.append({
            "doi": (r.get("DOI") or "").lower() or None,
            "title": r.get("article-title") or r.get("volume-title"),
            "author": author,
            "year": year,
            "journal": r.get("journal-title") or r.get("series-title"),
            "raw": raw,
            "source": "crossref",
        })
    return meta, refs


# ---------- PDF heuristics ----------

def _clean_doi(doi: str) -> str:
    doi = doi.rstrip(".)]}").strip().lower()
    return re.sub(r"\.s\d{3}$", "", doi)  # supplementary-material DOIs -> the article's DOI


def doi_candidates(meta: dict, text: str) -> list[tuple[str, bool]]:
    """(doi, trusted) pairs: DOIs in PDF metadata are trusted, DOIs in text need verification."""
    out = []
    for source, trusted in ((meta.get("/Subject"), True), (meta.get("/Keywords"), True),
                            (meta.get("/doi"), True), (text, False)):
        for m in DOI_RE.finditer(str(source or "")):
            doi = _clean_doi(m.group(1))
            if doi not in (d for d, _ in out):
                out.append((doi, trusted))
    return out[:6]


def _split_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = DOI_RE.sub("", raw)
    parts = re.split(r"[;,]|\band\b", raw)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) < 60]


def _split_authors(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r",|;|\band\b", raw)
    return [p.strip() for p in parts if p.strip()]


def _abstract_from_text(text: str) -> str | None:
    m = re.search(r"\bAbstract\b[\s.:—-]*(.{200,2500}?)(?:\n\s*\n|\b(?:Keywords|Key words|1\.?\s+Introduction|Unified Astronomy)\b)",
                  text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def _matches_hint(hint: tuple[str, int] | None, cr_meta: dict) -> bool:
    """Filename author/year vs a Crossref record; tolerant of umlaut spelling (oe/ö) and online/print years."""
    if not hint or not cr_meta["first_author_family"] or not cr_meta["year"]:
        return False
    fold = lambda s: re.sub(r"([aou])e", r"\1", s)
    return fold(hint[0]) == fold(norm(cr_meta["first_author_family"])) and abs(hint[1] - cr_meta["year"]) <= 1


def verified_work(candidates: list[tuple[str, bool]], page1: str, head: str, hint: tuple[str, int] | None = None,
                  lookup=crossref_work) -> tuple[str | None, dict | None]:
    """The first (doi, Crossref record) among `candidates` that really describes this PDF."""
    page1_norm, head_norm = norm(page1), norm(head)
    for doi, trusted in candidates:
        msg = lookup(doi)
        if not msg:
            continue
        cr_meta, _ = parse_crossref(msg)
        title_norm = norm(cr_meta["title"])[:60]  # cleaned: raw titles can contain <sub> markup
        if len(title_norm) <= 15:
            continue
        # Text DOIs may belong to cited works: the record's title must be on page 1, or on
        # page 2 with the record's first author and year matching the filename.
        on_page1 = title_norm in page1_norm
        on_head = title_norm in head_norm and _matches_hint(hint, cr_meta)
        if trusted or on_page1 or on_head:
            return doi, msg
    return None, None


def extract_metadata(path: Path, bib: dict | None = None, use_network: bool = True) -> dict:
    """Return paper fields plus `_refs` (reference list) for the scanner to store."""
    reader = PdfReader(path)
    meta = {k: v for k, v in (reader.metadata or {}).items()}
    head = read_text(path, max_pages=2)
    # A paper's own DOI and title are on its first page; later pages cite other works.
    page1 = read_text(path, max_pages=1)

    result = {
        "title": None, "authors": [], "first_author_family": None, "year": None,
        "journal": None, "doi": None, "abstract": None, "keywords": [],
        "pages": len(reader.pages), "metadata_source": "filename", "_refs": [],
    }

    # Filename hints like "Ryan2020 - Title.pdf" or "Reach2023.pdf"
    fm = re.match(r"^\s*([A-Za-zÀ-ÿ'\-]+?)[\s_]*((?:19|20)\d\d)[a-z]?\s*(?:[-–_:]\s*(.+))?$", path.stem)
    filename_hint = (norm(fm.group(1)), int(fm.group(2))) if fm else None
    if fm:
        result["authors"] = [fm.group(1)]
        result["first_author_family"] = fm.group(1)
        result["year"] = int(fm.group(2))
        if fm.group(3):
            result["title"] = fm.group(3).strip()
    else:
        result["title"] = path.stem

    # Embedded PDF metadata
    pdf_title = str(meta.get("/Title") or "").strip()
    # Producers often leave a filename or "Microsoft Word - ..." in the Title field.
    if len(pdf_title) > 5 and not re.search(r"\.(pdf|docx?|tex|dvi|indd)$|^microsoft word|^untitled", pdf_title, re.I):
        result["title"] = str(meta["/Title"]).strip()
        result["metadata_source"] = "pdf"
    authors = _split_authors(str(meta.get("/Author") or ""))
    if authors:
        result["authors"] = authors
        result["first_author_family"] = authors[0].split()[-1]
        result["metadata_source"] = "pdf"
    result["keywords"] = _split_keywords(str(meta.get("/Keywords") or ""))
    if not result["year"]:
        m = YEAR_RE.search(str(meta.get("/Subject") or "")) or YEAR_RE.search(head[:3000])
        if m:
            result["year"] = int(m.group(1))
    result["abstract"] = _abstract_from_text(head)

    # Curated BibTeX entry
    if bib:
        for k in ("title", "authors", "first_author_family", "year", "journal"):
            if bib.get(k):
                result[k] = bib[k]
        result["metadata_source"] = "bibtex"

    if not use_network:
        result["first_author"] = result["authors"][0] if result["authors"] else None
        return result

    # Resolve a DOI and pull the Crossref record
    candidates = ([(bib["doi"].lower(), True)] if bib and bib.get("doi") else []) + doi_candidates(meta, head)
    result["doi"], work = verified_work(candidates, page1, head, filename_hint)
    if not work and result["title"] and len(result["title"].split()) >= 4 and result["year"]:
        # Title search also finds reviews and reprints of the same work, so the year must agree.
        doi = crossref_search(result["title"])
        work = crossref_work(doi) if doi else None
        if work and abs((parse_crossref(work)[0]["year"] or 0) - result["year"]) <= 1:
            result["doi"] = doi.lower()
        else:
            work = None

    if work:
        cr, refs = parse_crossref(work)
        for k in ("title", "year", "journal", "abstract", "cited_by_count"):
            if cr.get(k) and not (k in ("title", "year") and bib and bib.get(k)):
                result[k] = cr[k]
        if cr["authors"]:
            result["authors"] = cr["authors"]
            result["first_author_family"] = cr["first_author_family"]
        if cr["keywords"] and not result["keywords"]:
            result["keywords"] = cr["keywords"]
        result["_refs"] = refs
        result["metadata_source"] = "bibtex+crossref" if bib else "crossref"

    if result["doi"]:
        oa = openalex_work(result["doi"])
        if oa:
            result.update({k: v for k, v in oa.items() if v is not None})  # OpenAlex counts are broader than Crossref's

    result["first_author"] = result["authors"][0] if result["authors"] else None
    return result
