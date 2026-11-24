"""Per-document analysis through the local Claude Code CLI (`claude -p`).

One call per document returns topical tags, key contributions and methodology
bullets — plus a structured reference list for papers that have none from
Crossref/OpenAlex. Each call is given the library's current tag vocabulary so
that new tags reuse existing ones instead of near-duplicates.

The text is piped to `claude` on stdin with tools disabled, and a JSON schema
forces the reply shape. The model and the library's research field come from the
Settings panel; without a model, your Claude Code default is used.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import citations, config, db, extract

BODY_CHARS = 60_000   # abstract, introduction, methods: enough for contributions/methodology/tags
REFS_CHARS = 60_000   # the reference section, when references are requested
TIMEOUT_S = 600
WORKERS = 3
RELINK_EVERY = 10

status = {"running": False, "total": 0, "done": 0, "current": None, "errors": 0, "last_error": None, "stopped": None}
LIMIT_RE = re.compile(r"(session|usage|rate) limit|limit reached", re.IGNORECASE)
_lock = threading.Lock()

REF_SCHEMA = {
    "type": "object",
    "properties": {
        "author": {"type": "string", "description": "first author's surname"},
        "year": {"type": "string"},
        "title": {"type": "string"},
        "journal": {"type": "string"},
        "doi": {"type": "string"},
    },
    "required": ["author", "year"],
}


def schema(want_refs: bool) -> dict:
    props = {
        "tags": {"type": "array", "items": {"type": "string"}},
        "contributions": {"type": "array", "items": {"type": "string"}},
        "methodology": {"type": "array", "items": {"type": "string"}},
    }
    if want_refs:
        props["references"] = {"type": "array", "items": REF_SCHEMA}
    return {"type": "object", "properties": props, "required": list(props)}


PROMPT = """You are cataloguing a researcher's literature library{field}. The document below is a {kind}.

Return JSON with:
- "contributions": 4-5 bullet points, one sentence each, stating the key contributions or findings
  of this work. Be specific: name the method, dataset, quantity or result. Do not restate the abstract
  in general terms.{kind_hint}
- "methodology": 3-6 bullet points, one sentence each, describing how the work was done: data and
  instruments, models and numerical methods, experimental setup, key assumptions.
- "tags": 3-6 short lowercase topical tags.
  Existing tags in the library: {vocabulary}
  Reuse an existing tag whenever it covers the same concept (synonyms, singular/plural, abbreviations,
  broader/narrower wording). Only create a new tag for a concept none of the existing tags covers.{refs_task}

Title: {title}
Authors: {authors}
Year: {year}

Document text:
{text}
"""

KIND_HINTS = {
    "book": " For a book, list the main topics it covers and what it is most useful for.",
    "lecture slides": " For lecture slides, list the main topics and techniques taught.",
}

REFS_TASK = """
- "references": every entry of the document's reference list / bibliography, in order. For each give
  the first author's surname, the year, and the title, journal and DOI when they appear (empty string
  otherwise). The reference section is included at the end of the text below."""


def kind_of(paper: dict) -> str:
    top = (paper.get("folder") or "").split("/")[0].lower()
    if top == "books" or (paper.get("pages") or 0) > 150:
        return "book"
    if top == "slides":
        return "lecture slides"
    if "thesis" in top:
        return "thesis"
    return "research paper"


def needs_references(conn, paper: dict) -> bool:
    """Papers/theses with no reference list from Crossref or OpenAlex."""
    if kind_of(paper) not in ("research paper", "thesis"):
        return False
    has = conn.execute("SELECT 1 FROM citations WHERE paper_id = ? AND source != 'claude' LIMIT 1",
                       (paper["id"],)).fetchone()
    return not has and not paper.get("openalex_refs")


def claude_bin() -> str | None:
    return os.environ.get("KM_CLAUDE_BIN") or shutil.which("claude")


def model_name() -> str:
    return config.get("claude_model") or "Claude Code default"


def is_configured() -> bool:
    return claude_bin() is not None


def _document_text(text: str, want_refs: bool) -> str:
    if not want_refs or len(text) <= BODY_CHARS + REFS_CHARS:
        return text[: BODY_CHARS + (REFS_CHARS if want_refs else 0)]
    refs = citations.references_section(text)[:REFS_CHARS]
    return text[:BODY_CHARS] + "\n\n[...]\n\nREFERENCES\n" + refs


def run_claude(paper: dict, text: str, vocabulary: list[str], want_refs: bool) -> dict:
    binary = claude_bin()
    if not binary:
        raise RuntimeError("The `claude` CLI was not found on PATH (set KM_CLAUDE_BIN)")
    kind = kind_of(paper)
    field = config.get("research_field").strip()
    prompt = PROMPT.format(
        field=f" ({field})" if field else "",
        kind=kind,
        kind_hint=KIND_HINTS.get(kind, ""),
        vocabulary=", ".join(sorted(vocabulary)) or "(none yet)",
        refs_task=REFS_TASK if want_refs else "",
        title=paper.get("title") or paper["filename"],
        authors=", ".join(paper.get("authors") or []) or "unknown",
        year=paper.get("year") or "unknown",
        text=_document_text(text, want_refs),
    )
    cmd = [binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema(want_refs)),
           "--tools", "", "--no-session-persistence"]
    if config.get("claude_model"):
        cmd += ["--model", config.get("claude_model")]

    # Run outside the project so no project CLAUDE.md or settings leak into the prompt.
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=TIMEOUT_S, cwd=tempfile.gettempdir())
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result') or out.get('subtype')}")
    data = out.get("structured_output") or json.loads(out.get("result") or "{}")
    models = list((out.get("modelUsage") or {}).keys())
    data["model"] = models[0] if models else model_name()
    return data


def _clean_list(items) -> list[str]:
    return [re.sub(r"^[\s•\-–*]+", "", str(x)).strip() for x in items or [] if str(x).strip()]


def _refs(items) -> list[dict]:
    out = []
    for r in items or []:
        doi = extract.DOI_RE.search(r.get("doi") or "")
        year = re.search(r"(19|20)\d\d", r.get("year") or "")
        out.append({
            "author": (r.get("author") or "").strip() or None,
            "year": int(year.group(0)) if year else None,
            "title": (r.get("title") or "").strip() or None,
            "journal": (r.get("journal") or "").strip() or None,
            "doi": doi.group(1).rstrip(".").lower() if doi else None,
            "raw": None,
            "source": "claude",
        })
    return out


def vocabulary(conn) -> set[str]:
    return {t for p in db.list_papers(conn) for t in p["tags"]}


def analyze_one(paper_id: int, vocab: set[str] | None = None) -> dict:
    """Analyze one paper and store the result. `vocab` (shared, mutated) is the current tag set."""
    conn = db.connect()
    paper = db.get_paper(conn, paper_id)
    text = db.get_text(conn, paper_id) or ""
    if len(text.strip()) < 500:
        raise RuntimeError("no extractable text in this PDF (scanned image?)")
    want_refs = needs_references(conn, paper)
    with _lock:
        current = sorted(vocab if vocab is not None else vocabulary(conn))
    data = run_claude(paper, text, current, want_refs)

    tags = sorted({t.strip().lower() for t in data.get("tags", []) if t.strip()})
    with _lock:
        if vocab is not None:
            vocab.update(tags)
    fields = {
        "tags": sorted(set(paper["tags"]) | set(tags)),
        "contributions": _clean_list(data.get("contributions")),
        "methodology": _clean_list(data.get("methodology")),
        "analysis_model": data["model"],
        "analyzed_at": db.now(),
    }
    db.update_paper(conn, paper_id, fields)
    if want_refs and data.get("references"):
        db.replace_citations(conn, paper_id, _refs(data["references"]))
        citations.rebuild_links(conn)
    return db.get_paper(conn, paper_id)


def analyze_all(paper_ids: list[int]) -> None:
    with _lock:
        if status["running"]:
            return
        status.update(running=True, total=len(paper_ids), done=0, current=None, errors=0, last_error=None, stopped=None)
    try:
        vocab = vocabulary(db.connect())
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(analyze_one, pid, vocab): pid for pid in paper_ids}
            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                try:
                    p = fut.result()
                    status["current"] = p["filename"]
                except Exception as e:
                    status["errors"] += 1
                    status["last_error"] = str(e)[:300]
                    if LIMIT_RE.search(str(e)) and not status["stopped"]:
                        # Every remaining call would fail the same way; keep them for the next run.
                        status["stopped"] = str(e)[:300]
                        for f in futures:
                            f.cancel()
                status["done"] += 1
                if status["done"] % RELINK_EVERY == 0:
                    citations.rebuild_links(db.connect())
        citations.rebuild_links(db.connect())
    finally:
        status.update(running=False, current=None)


def analyze_in_background(paper_ids: list[int]) -> bool:
    if status["running"] or not paper_ids:
        return False
    threading.Thread(target=analyze_all, args=(paper_ids,), daemon=True).start()
    return True
