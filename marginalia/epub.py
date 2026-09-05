"""EPUB -> ordered paragraphs plus labelled chapter cut points.

The spine is ground truth for text order; the ToC is a set of labelled cut
points into it.  Two passes: flatten the linear spine into ONE ordered list of
block elements (keeping every index), then resolve each ToC ``href#frag`` onto a
position in that list.  The wild-EPUB "special cases" -- many chapters per spine
file, Calibre ``*_split_NNN.html``, dangling fragments, a lying ``playOrder``,
front matter -- fall out of that shape instead of each getting a branch.

stdlib ``zipfile`` + ``lxml`` only: not ebooklib/PyMuPDF (AGPL sec.13 reaches a
bot) and not html2text (GPL, and Markdown flattening destroys the indices).
"""

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lxml import etree, html

log = logging.getLogger("marginalia.epub")

_BLOCK = frozenset("p h1 h2 h3 h4 h5 h6 li dd dt blockquote pre figcaption".split())
_HEADING = frozenset("h1 h2 h3 h4 h5 h6".split())
_PART = re.compile(r"\s*(part|book|volume|section)\b", re.I)
_SPLIT = re.compile(r"_split_0*[1-9]\d*\.x?html?$", re.I)
# A stem that is nothing but these plus digits is a filename, not a title.
_NOISE = re.compile(r"ch(apter)?|part|section|index|text|split|body|page|\d+|[\W_]", re.I)
_PREFIX = re.compile(r"^(chapter|part|ch)(?=[\W_\d])[\W_]*", re.I)
_MERGE_WORDS = 250
# Only what Discord renders reliably, and we ship no converter: SVG/WEBP/GIF are "no cover".
_COVER_MAGIC = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG"}
_COVER_HREF = re.compile(r"(^|/)cover\.(jpe?g|png)$", re.I)
_COVER_MAX = 2 * 1024 * 1024  # a cover is 50-300KB; well under Discord's 8MB attachment cap
# Font obfuscation, not DRM: the IDPF algorithm and Adobe's older equivalent.
_OBFUSCATION = frozenset(
    ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC4")
)


@dataclass(frozen=True)
class Para:
    chapter_index: int
    para_index: int
    text: str
    char_start: int
    href: str
    anchor: str | None


@dataclass(frozen=True)
class Chapter:
    index: int
    title: str
    alt_title: str | None


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    chapters: list[Chapter]
    paras: list[Para]
    total_chars: int
    cover: bytes | None = None
    cover_mime: str | None = None


class EncryptedEpubError(Exception):
    """The EPUB is really encrypted (DRM), not merely font-obfuscated."""


def _iter(el, *names: str):
    """Namespace-agnostic descendant search -- OPF2/OPF3/NCX all differ."""
    want = frozenset(names)
    for e in el.iter():
        if isinstance(e.tag, str) and etree.QName(e).localname in want:
            yield e


def _first(it):
    return next(iter(it), None)


def _norm(base: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(base, unquote(href))).lstrip("/")


def _xml(zf: zipfile.ZipFile, name: str):
    try:
        return etree.fromstring(zf.read(name))  # noqa: S320 - local file, no network entities
    except (KeyError, etree.XMLSyntaxError):
        return None


def _body(zf: zipfile.ZipFile, name: str):
    """Content documents get the forgiving HTML parser: no namespaces, no strictness."""
    try:
        data = zf.read(name)
    except KeyError:
        return None
    if not data.strip():
        return None
    try:
        doc = html.document_fromstring(data)
    except etree.ParserError:
        return None
    body = doc.find("body")
    return doc if body is None else body


def _ident(el) -> str | None:
    return el.get("id") or el.get("name")


def _walk(el, flat: list, ids: dict, si: int, path: str) -> None:
    """Append block elements in document order; map every id to a block index.

    An id maps to the first block at or after it, which is why one rule covers
    the id sitting on the heading, on a wrapper div, or on an empty <a> before it.
    """
    if el is None:
        return
    for child in el:
        if not isinstance(child.tag, str):
            continue
        own = _ident(child)
        if own:
            ids.setdefault((si, own), len(flat))
        if child.tag in _BLOCK:
            text = " ".join(child.text_content().split())
            if text:
                flat.append((path, child.tag, child.get("id"), text))
            here = len(flat) - (1 if text else 0)
            for d in child.iter():
                inner = _ident(d)
                if inner:
                    ids.setdefault((si, inner), here)
        else:
            _walk(child, flat, ids, si, path)


def _check_encryption(zf: zipfile.ZipFile) -> None:
    root = _xml(zf, "META-INF/encryption.xml")
    if root is None:
        return
    algos = {e.get("Algorithm") for e in root.iter()} - {None}
    bad = sorted(algos - _OBFUSCATION)
    if bad:
        raise EncryptedEpubError(
            "this EPUB is DRM-encrypted (" + ", ".join(bad) + ") and cannot be parsed; "
            "please supply a DRM-free copy"
        )


def _resolve(href: str, base: str, spine_of: dict, ids: dict, starts: list, ends: list):
    file_part, _, frag = href.partition("#")
    si = spine_of.get(_norm(base, file_part)) if file_part else None
    if si is None or ends[si] == starts[si]:
        return None
    if not frag:
        return starts[si]
    loc = ids.get((si, unquote(frag)))
    # ponytail: a dangling fragment drops the label (its text joins the previous
    # chapter) rather than claiming the file top, where it would outrank a real
    # cut point; aim it at the file top if some book turns up needing that.
    return None if loc is None else min(loc, ends[si] - 1)


_Entries = tuple[list[tuple[str, str]], str]  # [(label, href)], base dir for those hrefs


def _nav_entries(zf, opf_dir: str, manifest: dict) -> _Entries:
    nav = _first(e for e in manifest.values() if "nav" in (e.get("properties") or "").split())
    if nav is None:
        return [], opf_dir
    path = _norm(opf_dir, nav.get("href") or "")
    doc = _body(zf, path)
    if doc is None:
        return [], opf_dir
    navs = list(doc.iter("nav"))
    pick = _first(n for n in navs if (n.get("epub:type") or "") == "toc")
    pick = pick if pick is not None else (navs[0] if navs else doc)
    # <span> entries and <a> with no href are group labels, not destinations.
    entries = [(" ".join(a.text_content().split()), a.get("href"))
               for a in pick.iter("a") if a.get("href")]
    return entries, posixpath.dirname(path)


def _ncx_item(opf, manifest: dict):
    spine = _first(_iter(opf, "spine"))
    ncx = manifest.get(spine.get("toc") if spine is not None else None)
    if ncx is not None:
        return ncx
    return _first(e for e in manifest.values()
                  if (e.get("media-type") or "").endswith("x-dtbncx+xml"))


def _ncx_entries(zf, opf_dir: str, ncx) -> _Entries:
    path = _norm(opf_dir, ncx.get("href") or "")
    root = _xml(zf, path)
    if root is None:
        return [], opf_dir
    entries = []
    for np in _iter(root, "navPoint"):  # nested navPoints flatten; playOrder is never read
        label, content = _first(_iter(np, "text")), _first(_iter(np, "content"))
        if content is not None and content.get("src"):
            text = (label.text or "").strip() if label is not None else ""
            entries.append((text, content.get("src")))
    return entries, posixpath.dirname(path)


def _toc_cuts(zf, opf, opf_dir, manifest, spine_of, ids, starts, ends) -> list[tuple[int, str]]:
    """(position, label) pairs from the EPUB3 nav doc, else the EPUB2 NCX."""
    entries, base = _nav_entries(zf, opf_dir, manifest)
    if not entries and (ncx := _ncx_item(opf, manifest)) is not None:
        entries, base = _ncx_entries(zf, opf_dir, ncx)
    return [(pos, t) for t, h in entries
            if (pos := _resolve(h, base, spine_of, ids, starts, ends)) is not None]


def _stem_title(path: str, n: int) -> str:
    """Presentable filename title: `ch_split_000` -> `Chapter 3`, `my-intro` -> `My Intro`."""
    stem = Path(path).stem
    if not _NOISE.sub("", stem):  # ch_split_000, part0004, index_split_002
        return f"Chapter {n}"
    return _PREFIX.sub("", stem).replace("_", " ").replace("-", " ").strip().title()


def _fallback_cuts(flat: list, spine_of: dict, starts: list, ends: list) -> list[tuple[int, str]]:
    """No usable ToC: in-document headings, else one chapter per spine file."""
    raw = [(i, b[3]) for i, b in enumerate(flat) if b[1] in ("h1", "h2")]
    if raw:
        return raw
    keep = [(starts[si], p) for p, si in spine_of.items()
            if ends[si] > starts[si] and not _SPLIT.search(p)]  # not per Calibre split
    # ponytail: fixed noise list; add a word if a real stem still lands as "Chapter N".
    return [(pos, _stem_title(p, n)) for n, (pos, p) in enumerate(keep, 1)]


def _normalise_cuts(raw: list[tuple[int, str]], flat: list) -> list[tuple[int, str]]:
    cuts: list[tuple[int, str]] = []
    for pos, title in sorted(raw, key=lambda c: c[0]):  # document order wins
        if not cuts or cuts[-1][0] != pos:
            cuts.append((pos, title.strip() or "Untitled"))
    if flat and (not cuts or cuts[0][0] != 0):
        # Front matter keeps its own chapter rather than contaminating chapter 1.
        cuts.insert(0, (0, "Front Matter"))
    return _merge_parts(cuts, flat)


def _merge_parts(cuts: list[tuple[int, str]], flat: list) -> list[tuple[int, str]]:
    """Fold a short "Part Two" heading into its first child chapter."""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(cuts):
        pos, title = cuts[i]
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(flat)
        words = sum(len(flat[j][3].split()) for j in range(pos, end))
        if i + 1 < len(cuts) and _PART.match(title) and words < _MERGE_WORDS:
            title = cuts[i + 1][1]  # keep the part's start, take the child's label
            i += 1
        out.append((pos, title))
        i += 1
    return out


def _open_package(zf: zipfile.ZipFile, path) -> tuple[object, str, dict, set[str]]:
    names = set(zf.namelist())
    if "mimetype" in names and zf.read("mimetype").strip() != b"application/epub+zip":
        raise ValueError(f"{path}: not an EPUB (bad mimetype)")
    container = _xml(zf, "META-INF/container.xml")
    rootfile = _first(_iter(container, "rootfile")) if container is not None else None
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError(f"{path}: no rootfile in META-INF/container.xml")
    opf_path = _norm("", rootfile.get("full-path"))
    opf = _xml(zf, opf_path)
    if opf is None:
        raise ValueError(f"{path}: unreadable OPF at {opf_path}")
    manifest = {e.get("id"): e for e in _iter(opf, "item") if e.get("id")}
    return opf, posixpath.dirname(opf_path), manifest, names


def _cover_item(opf, manifest: dict):
    """EPUB3 ``properties`` -> EPUB2 ``<meta name="cover">`` -> a cover-looking href."""
    item = _first(e for e in manifest.values()
                  if "cover-image" in (e.get("properties") or "").split())
    if item is not None:
        return item
    meta = _first(m for m in _iter(opf, "meta") if (m.get("name") or "").lower() == "cover")
    item = manifest.get(meta.get("content")) if meta is not None else None
    if item is not None:
        return item
    return _first(e for e in manifest.values() if _COVER_HREF.search(e.get("href") or "")
                  and (e.get("media-type") or "").startswith("image/"))


def _cover(zf, opf, opf_dir: str, manifest: dict) -> tuple[bytes | None, str | None]:
    """A cover is a nicety: every failure here is (None, None), never an exception."""
    try:
        item = _cover_item(opf, manifest)
        mime = (item.get("media-type") or "").strip().lower() if item is not None else ""
        magic = _COVER_MAGIC.get(mime)
        if magic is None:
            return None, None
        info = zf.getinfo(_norm(opf_dir, item.get("href") or ""))
        if info.file_size > _COVER_MAX:  # checked before read(), so no decompression bomb
            log.info("cover %s is %d bytes (> %d): storing no cover",
                     info.filename, info.file_size, _COVER_MAX)
            return None, None
        data = zf.read(info)
        if not data.startswith(magic):  # mislabelled entry: a broken attachment beats none
            log.info("cover %s claims %s but the magic bytes disagree", info.filename, mime)
            return None, None
        return data, mime
    except Exception:  # noqa: BLE001 - a bad cover must never fail an ingest
        log.info("cover extraction failed; continuing without one", exc_info=True)
        return None, None


def _flatten_spine(zf, opf, opf_dir, manifest, names):
    flat: list[tuple[str, str, str | None, str]] = []  # (href, tag, own id, text)
    ids: dict[tuple[int, str], int] = {}  # (spine idx, fragment) -> flat index
    starts, ends, spine_of = [], [], {}
    for ref in _iter(opf, "itemref"):
        if (ref.get("linear") or "yes").lower() == "no":  # never enters the flat list
            continue
        item = manifest.get(ref.get("idref"))
        doc_path = _norm(opf_dir, item.get("href") or "") if item is not None else ""
        if doc_path not in names or doc_path in spine_of:
            continue
        si = len(starts)
        spine_of[doc_path] = si
        starts.append(len(flat))
        _walk(_body(zf, doc_path), flat, ids, si, doc_path)
        ends.append(len(flat))
    return flat, ids, starts, ends, spine_of


def _build(cuts: list[tuple[int, str]], flat: list) -> tuple[list[Chapter], list[Para], int]:
    chapters: list[Chapter] = []
    paras: list[Para] = []
    char = 0  # monotonic across the whole book; para_index restarts per chapter
    for ci, (pos, title) in enumerate(cuts):
        end = cuts[ci + 1][0] if ci + 1 < len(cuts) else len(flat)
        head = flat[pos][3] if flat[pos][1] in _HEADING else None
        alt = head if head != title else None  # keep BOTH labels when they disagree
        chapters.append(Chapter(index=ci, title=title, alt_title=alt))
        for pi, j in enumerate(range(pos, end)):
            doc_path, _tag, anchor, text = flat[j]
            paras.append(Para(ci, pi, text, char, doc_path, anchor))
            char += len(text)
    return chapters, paras, char


def parse(path: str | Path) -> Book:
    """mimetype -> container.xml -> OPF -> spine text order -> ToC cut points."""
    with zipfile.ZipFile(path) as zf:
        _check_encryption(zf)
        opf, opf_dir, manifest, names = _open_package(zf, path)
        flat, ids, starts, ends, spine_of = _flatten_spine(zf, opf, opf_dir, manifest, names)

        raw = _toc_cuts(zf, opf, opf_dir, manifest, spine_of, ids, starts, ends)
        cuts = _normalise_cuts(raw or _fallback_cuts(flat, spine_of, starts, ends), flat)

        chapters, paras, char = _build(cuts, flat)
        meta = {n: _first(e.text.strip() for e in _iter(opf, n) if e.text and e.text.strip())
                for n in ("title", "creator")}
        cover, cover_mime = _cover(zf, opf, opf_dir, manifest)
    return Book(meta["title"] or Path(path).stem, meta["creator"] or "Unknown",
                chapters, paras, char, cover, cover_mime)
