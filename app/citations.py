"""Resolve stored references to papers in the library and rebuild citation links.

A link citing -> cited is recorded with the strongest method that found it:
  doi         reference DOI equals a library paper's DOI
  openalex    OpenAlex lists the library paper among the citing paper's references
  title       a library paper's title appears in the reference entry
  pdf         library DOI or title found in the citing PDF's reference section
              (used only when no structured reference list is available)
  author-year first-author surname + year match exactly one library paper (weakest)

Links to a paper published more than a year after the citing paper are dropped.
"""

import json
import re
import unicodedata
from collections import defaultdict

from .extract import norm

METHOD_RANK = {"doi": 0, "openalex": 1, "title": 2, "pdf": 3, "author-year": 4}
MIN_TITLE_LEN = 25  # normalized chars; shorter titles ("Introduction") match too much


def _words(s: str | None) -> set[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z]{4,}", s)}


def _ref_title_words(c) -> set[str] | None:
    """Words of the reference's title, or None if the entry carries no title (e.g. AAS style)."""
    if c["title"]:
        return _words(c["title"])
    # A title shows up as a run of lowercase words; author lists and journal abbreviations don't.
    lower = re.findall(r"\b[a-z]{4,}\b", c["raw"] or "")
    return _words(c["raw"]) if len(lower) >= 4 else None


def references_section(text: str) -> str:
    """Text after the last References/Bibliography heading (or the last third as a fallback)."""
    hits = list(re.finditer(r"^\s*(references( cited)?|bibliography|literature cited)\s*$",
                            text, re.IGNORECASE | re.MULTILINE))
    return text[hits[-1].end():] if hits else text[len(text) * 2 // 3:]


def rebuild_links(conn) -> int:
    papers = [dict(r) for r in conn.execute(
        "SELECT id, doi, title, first_author_family, year, openalex_id, openalex_refs, metadata_source, missing"
        " FROM papers")]
    papers = [p for p in papers if not p["missing"]]
    by_id = {p["id"]: p for p in papers}

    by_doi = {p["doi"].lower(): p["id"] for p in papers if p["doi"]}
    by_oa = {p["openalex_id"]: p["id"] for p in papers if p["openalex_id"]}
    titles = [(norm(p["title"]), p["id"]) for p in papers if len(norm(p["title"])) >= MIN_TITLE_LEN]
    # Full-text matching is noisy, so only target papers whose title was verified externally.
    verified_titles = [(t, pid) for t, pid in titles
                       if any(s in (by_id[pid]["metadata_source"] or "") for s in ("crossref", "bibtex"))]
    by_author_year = defaultdict(list)
    for p in papers:
        if p["first_author_family"] and p["year"]:
            by_author_year[(norm(p["first_author_family"]), p["year"])].append(p["id"])

    def plausible(citing, cited):
        a, b = by_id.get(citing, {}).get("year"), by_id.get(cited, {}).get("year")
        return citing != cited and not (a and b and b > a + 1)

    links: dict[tuple[int, int], str] = {}

    def add(citing, cited, method):
        if not plausible(citing, cited):
            return False
        old = links.get((citing, cited))
        if old is None or METHOD_RANK[method] < METHOD_RANK[old]:
            links[(citing, cited)] = method
        return True

    def match(c):
        if c["doi"] and c["doi"].lower() in by_doi:
            return by_doi[c["doi"].lower()], "doi"
        entry = norm(c["title"]) + norm(c["raw"])
        for t, pid in titles:
            if t in entry:
                return pid, "title"
        if c["author"] and c["year"] and not c["doi"]:  # a DOI outside the library is some other work
            surname = norm(re.split(r"[\s,]", c["author"].strip())[0])
            ids = by_author_year.get((surname, c["year"]), [])
            if len(ids) == 1:
                ref_words = _ref_title_words(c)
                cand_words = _words(by_id[ids[0]]["title"])
                # Same author and year but a visibly different title is a different work.
                if ref_words is None or not cand_words or len(ref_words & cand_words) / len(cand_words) >= 0.4:
                    return ids[0], "author-year"
        return None, None

    # 1. Structured reference lists
    resolved, has_refs = [], set()
    for c in conn.execute("SELECT id, paper_id, doi, title, author, year, raw FROM citations"):
        has_refs.add(c["paper_id"])
        cited, method = match(c)
        if cited and not add(c["paper_id"], cited, method):
            cited = None
        resolved.append((cited, c["id"]))
    conn.executemany("UPDATE citations SET cited_paper_id = ? WHERE id = ?", resolved)

    # 2. OpenAlex referenced works
    for p in papers:
        refs = json.loads(p["openalex_refs"] or "[]")
        if refs:
            has_refs.add(p["id"])
        for w in refs:
            if w in by_oa:
                add(p["id"], by_oa[w], "openalex")

    # 3. Full-text fallback for papers without any structured references
    for p in papers:
        if p["id"] in has_refs:
            continue
        row = conn.execute("SELECT text FROM texts WHERE paper_id = ?", (p["id"],)).fetchone()
        if not row or not row["text"]:
            continue
        section = references_section(row["text"])
        section_lower, section_norm = section.lower(), norm(section)
        for doi, pid in by_doi.items():
            if doi in section_lower:
                add(p["id"], pid, "pdf")
        for t, pid in verified_titles:
            if t in section_norm:
                add(p["id"], pid, "pdf")

    conn.execute("DELETE FROM citation_links")
    conn.executemany("INSERT INTO citation_links (citing_id, cited_id, method) VALUES (?, ?, ?)",
                     [(a, b, m) for (a, b), m in links.items()])
    conn.commit()
    return len(links)
