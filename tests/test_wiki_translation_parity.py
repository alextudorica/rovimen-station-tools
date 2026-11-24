"""The build guide ships as two whole templates, English and Romanian.

They are separate files on purpose (see ``WIKI_TEMPLATES`` in
``dashboard/routes/misc.py``), so nothing else stops them drifting apart: a
section added to one, a photo swapped in one, a price updated in one. These
tests pin the parts a reader compares across languages to be identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "dashboard" / "templates"
STATIC = REPO / "dashboard" / "static"
EN = (TEMPLATES / "wiki.html").read_text(encoding="utf-8")
RO = (TEMPLATES / "wiki_ro.html").read_text(encoding="utf-8")

_STATIC_REF = re.compile(r"url_for\('static', filename='([^']+)'")


def _section_ids(html: str) -> list[str]:
    return re.findall(r'<section id="([^"]+)"', html)


def _toc_targets(html: str) -> list[str]:
    toc = re.search(r'<aside class="wk-toc".*?</aside>', html, re.S)
    assert toc, "table of contents missing"
    return re.findall(r'href="#([^"]+)"', toc.group(0))


def _media(html: str) -> list[str]:
    """Every image the page loads, with the Romanian diagram suffix removed."""
    return [ref.replace(".ro.svg", ".svg")
            for ref in _STATIC_REF.findall(html) if ref.startswith("media/")]


def _cost_cards(html: str) -> list[tuple[list[int], list[int]]]:
    """Per cost card: its line-item amounts and the numbers in its total line."""
    section = re.search(r'<section id="cost">.*?</section>', html, re.S)
    assert section, "cost section missing"
    cards = []
    for card in re.findall(r'<div class="wk-card[^"]*">.*?<p class="wk-cost-total">[^<]*</p>',
                           section.group(0), re.S):
        items = [int(n) for n in re.findall(r"<li><span>[^<]*</span><span>(\d+)</span></li>", card)]
        total = [int(n) for n in re.findall(r"\d+", re.search(r'<p class="wk-cost-total">([^<]*)</p>', card).group(1))]
        cards.append((items, total))
    return cards


def _rate(html: str) -> float:
    """The EUR/RON rate the page states, written 5.2788 (EN) or 5,2788 (RO)."""
    m = re.search(r"1&nbsp;EUR = (\d+[.,]\d+)&nbsp;(?:RON|lei)", html)
    assert m, "exchange rate missing"
    return float(m.group(1).replace(",", "."))


def test_same_sections_in_the_same_order():
    assert _section_ids(EN) == _section_ids(RO)


def test_table_of_contents_links_every_section():
    for html in (EN, RO):
        assert _toc_targets(html) == _section_ids(html)


def test_same_photos_and_diagrams_in_the_same_order():
    assert _media(EN) == _media(RO)


def test_cost_figures_agree_through_the_exchange_rate():
    """EN prices are in EUR and RO prices in lei, one card per build.

    Each EN item is its RO item converted at the stated rate (to within
    rounding), each total is the sum of its own items, and each total's
    bracketed other-currency figure is the other page's total.
    """
    rate = _rate(EN)
    assert _rate(RO) == rate
    en, ro = _cost_cards(EN), _cost_cards(RO)
    assert len(en) == len(ro) and en
    for (en_items, en_total), (ro_items, ro_total) in zip(en, ro):
        assert len(en_items) == len(ro_items)
        for eur, lei in zip(en_items, ro_items):
            assert abs(eur - lei / rate) <= 1, (eur, lei)
        assert en_total == [sum(en_items), sum(ro_items)]
        assert ro_total == [sum(ro_items), sum(en_items)]


@pytest.mark.parametrize("html", [EN, RO], ids=["en", "ro"])
def test_every_referenced_static_file_exists(html: str):
    missing = [ref for ref in _STATIC_REF.findall(html) if not (STATIC / ref).is_file()]
    assert missing == []


@pytest.mark.parametrize("html", [EN, RO], ids=["en", "ro"])
def test_photos_reserve_their_space(html: str):
    """width/height on every photo keep lazy loading from shifting the text."""
    for tag in re.findall(r'<img class="wk-photo"[^>]*>', html, re.S):
        assert re.search(r'width="\d+"', tag) and re.search(r'height="\d+"', tag), tag
