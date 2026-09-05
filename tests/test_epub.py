"""EPUB shapes that break naive parsers, plus the DRM boundary.

The NCX / part-merge / spine-fallback tests are CHARACTERIZATION tests: they pin
what the parser does today, not what it ideally would.
"""
import zipfile

import pytest

from marginalia.epub import EncryptedEpubError, parse

CONTAINER = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
ENC = '<encryption xmlns="urn:x"><EncryptionMethod Algorithm="%s"/></encryption>'


JPEG = b"\xff\xd8\xff" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _cover_href(how):
    return "cover.jpg" if how == "href" else "art/front.jpg"


def mk(tmp_path, docs, toc=None, enc=None, ncx=None, cover=None):
    """docs: [(filename, body html)]. toc: nav <li> markup. ncx: navMap <navPoint> markup.

    cover: (how, media-type, bytes) where `how` picks the fallback rung being
    exercised -- 'epub3' properties, 'epub2' <meta name="cover">, or 'href' alone.
    """
    items = "".join(f'<item id="d{i}" href="{f}" media-type="application/xhtml+xml"/>'
                    for i, (f, _) in enumerate(docs))
    meta = ""
    if cover:
        how, mime, _data = cover
        # Only the 'href' rung gets a cover-looking name, so each rung is proven alone.
        props = ' properties="cover-image"' if how == "epub3" else ""
        items += f'<item id="cov" href="{_cover_href(how)}" media-type="{mime}"{props}/>'
        meta = '<meta name="cover" content="cov"/>' if how == "epub2" else ""
    if toc:
        items += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" ' \
                 'properties="nav"/>'
    if ncx:
        items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    spine = '<spine toc="ncx">' if ncx else "<spine>"
    opf = ('<package xmlns="http://www.idpf.org/2007/opf"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
           f'<dc:creator>A</dc:creator>{meta}</metadata><manifest>{items}</manifest>{spine}'
           + "".join(f'<itemref idref="d{i}"/>' for i in range(len(docs))) + "</spine></package>")
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        if enc:
            z.writestr("META-INF/encryption.xml", enc)
        z.writestr("OEBPS/content.opf", opf)
        for f, body in docs:
            z.writestr(f"OEBPS/{f}", f"<html><body>{body}</body></html>")
        if toc:
            z.writestr("OEBPS/nav.xhtml",
                       f'<html><body><nav epub:type="toc"><ol>{toc}</ol></nav></body></html>')
        if ncx:
            z.writestr("OEBPS/toc.ncx", '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                                        f"<navMap>{ncx}</navMap></ncx>")
        if cover:
            z.writestr(f"OEBPS/{_cover_href(cover[0])}", cover[2])
    return str(p)


def navpoint(order, label, src, inner=""):
    return (f'<navPoint playOrder="{order}"><navLabel><text>{label}</text></navLabel>'
            f'<content src="{src}"/>{inner}</navPoint>')


def check(book):
    assert [p.char_start for p in book.paras] == sorted({p.char_start for p in book.paras})
    for ch in book.chapters:
        idx = [p.para_index for p in book.paras if p.chapter_index == ch.index]
        assert idx == list(range(len(idx)))
    return book


def test_fragment_addressed(tmp_path):
    body = "".join(f'<div id="w{n}"><h2>Head {n}</h2><p>text of {n}</p></div>' for n in (1, 2, 3))
    book = check(parse(mk(tmp_path, [("all.xhtml", "<p>cover blurb</p>" + body)],
                          '<li><span>Group label</span></li>'
                          '<li><a href="all.xhtml#w1">One</a></li>'
                          '<li><a href="all.xhtml#w2">Head 2</a></li>'
                          '<li><a href="all.xhtml#nope">Three</a></li>'
                          '<li><a href="all.xhtml#w3">Three</a></li>')))
    # span label and dangling #nope contribute no cut; the cover blurb is not chapter 1.
    assert [c.title for c in book.chapters] == ["Front Matter", "One", "Head 2", "Three"]
    assert [c.alt_title for c in book.chapters] == [None, "Head 1", None, "Head 3"]
    assert [p.text for p in book.paras if p.chapter_index == 2] == ["Head 2", "text of 2"]
    assert book.total_chars == sum(len(p.text) for p in book.paras)


def test_calibre_split(tmp_path):
    book = check(parse(mk(tmp_path, [
        ("c_split_000.xhtml", '<h2 id="c1">Ch One</h2><p>first half</p>'),
        ("c_split_001.xhtml", "<p>second half</p>"),
        ("c_split_002.xhtml", '<h2 id="c2">Ch Two</h2><p>later</p>'),
    ], '<li><a href="c_split_000.xhtml#c1">Ch One</a></li>'
       '<li><a href="c_split_002.xhtml#c2">Ch Two</a></li>')))
    assert [c.title for c in book.chapters] == ["Ch One", "Ch Two"]
    assert [p.chapter_index for p in book.paras] == [0, 0, 0, 1, 1]


def test_no_toc_falls_back_to_headings(tmp_path):
    book = check(parse(mk(tmp_path, [("a.xhtml", "<h1>Alpha</h1><p>one</p>"),
                                     ("b.xhtml", "<h1>Beta</h1><p>two</p>")])))
    assert [c.title for c in book.chapters] == ["Alpha", "Beta"]


def test_epub2_ncx_document_order_beats_playorder(tmp_path):
    # playOrder is scrambled and the second entry is a NESTED navPoint; neither shows.
    book = check(parse(mk(tmp_path, [
        ("a.xhtml", '<h1 id="s1">Head A</h1><p>alpha</p><h2 id="s2">Head A2</h2><p>sub</p>'),
        ("b.xhtml", "<h1>Head B</h1><p>beta</p>"),
    ], ncx=navpoint(3, "Ncx Alpha", "a.xhtml#s1",
                    navpoint(1, "Ncx Alpha Sub", "a.xhtml#s2"))
       + navpoint(2, "Ncx Beta", "b.xhtml"))))
    # Titles come from the NCX, not from the headings: proves the NCX branch ran.
    assert [c.title for c in book.chapters] == ["Ncx Alpha", "Ncx Alpha Sub", "Ncx Beta"]
    assert [c.alt_title for c in book.chapters] == ["Head A", "Head A2", "Head B"]
    assert [p.chapter_index for p in book.paras] == [0, 0, 1, 1, 2, 2]


def test_short_part_heading_folds_into_child(tmp_path):
    toc = '<li><a href="a.xhtml#p2">Part Two</a></li><li><a href="a.xhtml#c1">Chapter One</a></li>'
    body = '<h1 id="p2">Part Two</h1><h2 id="c1">Chapter One</h2><p>%s</p>'
    book = check(parse(mk(tmp_path, [("a.xhtml", body % "short")], toc)))
    assert [(c.title, c.alt_title) for c in book.chapters] == [("Chapter One", "Part Two")]
    # _MERGE_WORDS counts only the part's OWN span (heading -> first child), not the
    # child's text: 250 words of child body still merges, a 250-word part blurb does not.
    assert check(parse(mk(tmp_path, [("a.xhtml", body % ("w " * 250))], toc))).chapters[0].title \
        == "Chapter One"
    blurb = '<h1 id="p2">Part Two</h1><p>%s</p><h2 id="c1">Chapter One</h2><p>x</p>' % ("w " * 250)
    assert [c.title for c in check(parse(mk(tmp_path, [("a.xhtml", blurb)], toc))).chapters] \
        == ["Part Two", "Chapter One"]


def test_no_toc_no_headings_falls_back_to_spine_files(tmp_path):
    book = check(parse(mk(tmp_path, [("front.xhtml", "<p>blurb</p>"),
                                     ("ch_split_000.xhtml", "<p>one</p>"),
                                     ("ch_split_001.xhtml", "<p>two</p>")])))
    # Tidied stems, positional title for a filename-looking one (was "ch_split_000");
    # _split_001 is a Calibre continuation so it starts no chapter.
    assert [c.title for c in book.chapters] == ["Front", "Chapter 2"]
    assert [p.chapter_index for p in book.paras] == [0, 1, 1]


DOC = [("a.xhtml", "<h1>Alpha</h1><p>one</p>")]


@pytest.mark.parametrize("how, mime, data", [
    ("epub3", "image/jpeg", JPEG),   # EPUB3 properties="cover-image"
    ("epub3", "image/png", PNG),
    ("epub2", "image/jpeg", JPEG),   # EPUB2 <meta name="cover" content="cov">
    ("href", "image/jpeg", JPEG),    # last rung: a cover-looking href, image media-type
])
def test_cover_fallback_chain(tmp_path, how, mime, data):
    book = parse(mk(tmp_path, DOC, cover=(how, mime, data)))
    assert (book.cover, book.cover_mime) == (data, mime)


@pytest.mark.parametrize("cover", [
    None,                                            # no cover at all: the normal case
    ("epub3", "image/svg+xml", b"<svg/>"),           # Discord-unreliable format
    ("epub3", "image/gif", b"GIF89a"),
    ("epub3", "image/jpeg", PNG),                    # mislabelled: bytes are a PNG
    ("epub3", "image/png", b"not an image at all"),
    ("epub3", "image/jpeg", JPEG + b"\x00" * 2 * 1024 * 1024),  # over the 2MiB cap
])
def test_no_usable_cover_is_none_and_still_parses(tmp_path, cover):
    book = parse(mk(tmp_path, DOC, cover=cover))
    assert (book.cover, book.cover_mime) == (None, None)
    assert [c.title for c in book.chapters] == ["Alpha"]  # the rest of the parse is untouched


def test_drm_vs_font_obfuscation(tmp_path):
    doc = [("a.xhtml", "<h1>Alpha</h1><p>one</p>")]
    with pytest.raises(EncryptedEpubError):
        parse(mk(tmp_path, doc, enc=ENC % "http://www.w3.org/2001/04/xmlenc#aes128-cbc"))
    assert parse(mk(tmp_path, doc, enc=ENC % "http://www.idpf.org/2008/embedding")).chapters
