"""BibTeX entries for the library, built from each paper's stored metadata.

Citation keys look like `Hayne2017global` (first author, year, first significant title word) and are
made unique over the whole library, so a copied entry and the full export agree.
"""

import re
import unicodedata

from . import db

PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "di", "du", "le", "la", "dos", "das", "ter"}
# Words that mark an organisation rather than a person, e.g. "Space Studies Board".
ORG_WORDS = {"board", "committee", "council", "academies", "academy", "division", "survey", "society", "institute",
             "agency", "university", "team", "consortium", "collaboration", "group", "nasa", "esa", "organization",
             "association", "laboratory", "center", "centre", "office", "department"}
STOPWORDS = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "from", "with", "by", "at", "towards", "toward"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _greek(ch: str) -> str:
    """Greek letters as LaTeX math, which pdflatex accepts (unlike the Unicode characters)."""
    name = unicodedata.name(ch, "")
    m = re.fullmatch(r"(?:GREEK (SMALL|CAPITAL) LETTER|MICRO SIGN)\s*(\w*)", name)
    if not m:
        return ch
    letter = (m.group(2) or "MU").lower().replace("lamda", "lambda")
    return "{$\\" + (letter.capitalize() if m.group(1) == "CAPITAL" else letter) + "$}"


def _escape(s: str) -> str:
    """Make a value safe inside a braced BibTeX field: escape LaTeX specials, drop stray braces."""
    s = re.sub(r"\s+", " ", str(s)).strip().replace("\\", "")
    s = s.replace("\u2010", "-").replace("\u2011", "-")  # Unicode hyphens break classic pdflatex
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"([&%$#_])", r"\\\1", s)
    return re.sub(r"[\u0370-\u03ff\u00b5]", lambda m: _greek(m.group()), s)


def _bib_name(name: str) -> str:
    """'Paul O. Hayne' -> 'Hayne, Paul O.'; 'Jan van der Berg' -> 'van der Berg, Jan'.

    Organisations, and strings holding several people at once (from messy PDF metadata), are
    braced so BibTeX prints them as they are instead of failing to split them into names."""
    name = name.strip()
    parts = name.split()
    words = {w.lower().strip(",.") for w in parts}
    if "&" in name or name.count(",") > 1 or words & ORG_WORDS or len(parts) > 5:
        return "{" + name + "}"
    if "," in name:  # already "Family, Given"
        return name
    if len(parts) < 2:
        return name
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return f"{' '.join(parts[i:])}, {' '.join(parts[:i])}"


def _name_field(name: str) -> str:
    bib = _bib_name(name)
    braced = bib.startswith("{")
    text = _escape(bib[1:-1] if braced else bib)  # _escape drops braces, so re-add the protecting pair
    return "{" + text + "}" if braced else text


def _key_base(p: dict) -> str:
    family = p.get("first_author_family") or (p["authors"][0].split()[-1] if p.get("authors") else "") or "anon"
    family = re.sub(r"[^A-Za-z]", "", _ascii(family)) or "anon"
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(p.get("title") or "").lower()) if w not in STOPWORDS]
    return f"{family}{p.get('year') or ''}{words[0] if words else ''}"


def _entry_type(p: dict) -> str:
    top = (p.get("folder") or "").split("/")[0].lower()
    if top == "books":
        return "book"
    if "thesis" in top:
        return "mastersthesis"
    return "article" if p.get("journal") else "misc"


def entry(p: dict, key: str) -> str:
    kind = _entry_type(p)
    fields = []
    if p.get("authors"):
        fields.append(("author", " and ".join(_name_field(a) for a in p["authors"])))
    # Double braces keep the title's capitalization in every bibliography style.
    fields.append(("title", "{" + _escape(p.get("title") or p["filename"].removesuffix(".pdf")) + "}"))
    if p.get("journal"):
        fields.append(("publisher" if kind == "book" else "journal", _escape(p["journal"])))
    for field, column in (("volume", "volume"), ("number", "issue"), ("pages", "page_range")):
        if p.get(column) and not (field == "pages" and kind == "book"):  # a book's "pages" is a chapter's
            value = _escape(p[column])
            if field == "pages":  # "123-145" -> "123--145", BibTeX's page range dash
                value = re.sub(r"^(\w+)\s*[-\u2013\u2014]{1,2}\s*(\w+)$", r"\1--\2", value)
            fields.append((field, value))
    if p.get("year"):
        fields.append(("year", str(p["year"])))
    if p.get("doi"):
        fields.append(("doi", _escape(p["doi"]).replace("\\_", "_")))  # DOIs go verbatim to the doi package
    width = max(len(k) for k, _ in fields)
    body = ",\n".join(f"  {k.ljust(width)} = {{{v}}}" for k, v in fields)
    return f"@{kind}{{{key},\n{body}\n}}\n"


def keys(papers: list[dict]) -> dict[int, str]:
    """Unique citation key per paper id; papers sharing a base key get a, b, c… in id order."""
    by_base: dict[str, list[int]] = {}
    for p in sorted(papers, key=lambda p: p["id"]):
        by_base.setdefault(_key_base(p), []).append(p["id"])
    out = {}
    for base, ids in by_base.items():
        for i, pid in enumerate(ids):
            out[pid] = base if len(ids) == 1 else base + "abcdefghijklmnopqrstuvwxyz"[i % 26] + ("" if i < 26 else str(i // 26))
    return out


def _papers(conn) -> list[dict]:
    return [p for p in db.list_papers(conn) if not p["missing"]]


def for_paper(conn, paper_id: int) -> str | None:
    papers = _papers(conn)
    p = next((p for p in papers if p["id"] == paper_id), None)
    return entry(p, keys(papers)[paper_id]) if p else None


def library(conn) -> str:
    papers = _papers(conn)
    k = keys(papers)
    papers.sort(key=lambda p: k[p["id"]].lower())
    return "\n".join(entry(p, k[p["id"]]) for p in papers)
