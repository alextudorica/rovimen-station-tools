#!/usr/bin/env python3
"""Find the English Wikipedia article for each IAU meteor shower.

Looks each shower name up through the MediaWiki API (several spellings:
"eta-Aquariids" is "Eta Aquariids" on Wikipedia), follows redirects, and
keeps a match only when the article is in a meteor-shower category, so a
name that lands on a constellation, a comet or a disambiguation page is
dropped. Writes {IAU code: article title}; the page links
https://en.wikipedia.org/wiki/<title>.

Usage:
    uv run tools/build_shower_wikipedia.py --showers showers.json \
        --output dashboard/shower_data/wikipedia.json
`--showers` is a saved /api/showers response ({code: {"name": ...}}).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "ROVIMEN-dashboard/1.0 (shower links; https://github.com/alextudorica/rovimen-station-tools)"}


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
PREFIXES = MONTHS + ("Northern", "Southern", "Daytime")
GENERAL = ("Meteor shower", "List of meteor showers")   # not one shower's article


def variants(name: str) -> list[str]:
    """Spellings to try, most likely first: the IAU name, then without a
    leading month or branch ("April Lyrids" is "Lyrids", "Southern
    delta-Aquariids" is "Delta Aquariids" on Wikipedia)."""
    n = " ".join(name.split())
    bases = [n]
    first, _, rest = n.partition(" ")
    if first in PREFIXES and rest:
        bases.append(rest)
    out = []
    for b in bases:
        for v in (b, b.replace("-", " ")):
            out += [v, v[:1].upper() + v[1:]]
    return list(dict.fromkeys(out))


def lookup(titles: list[str], session: requests.Session) -> dict[str, str]:
    """{asked title: final article title} for titles whose article (after
    redirects) is in a meteor-shower category."""
    r = session.get(API, params={
        "action": "query", "format": "json", "redirects": 1, "titles": "|".join(titles),
        "prop": "categories", "cllimit": "max", "clshow": "!hidden",
    }, headers=HEADERS, timeout=30)
    r.raise_for_status()
    q = r.json().get("query", {})
    hop = {}
    for key in ("normalized", "redirects"):
        for x in q.get(key, []):
            hop[x["from"]] = x["to"]
    good = set()
    for page in q.get("pages", {}).values():
        if "missing" in page:
            continue
        cats = " ".join(c["title"] for c in page.get("categories", [])).lower()
        if "meteor shower" in cats:
            good.add(page["title"])
    found = {}
    for t in titles:
        final = t
        for _ in range(3):
            final = hop.get(final, final)
        if final in good:
            found[t] = final
    return found


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--showers", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    showers = json.loads(a.showers.read_text())
    norm = lambda t: " ".join(t.replace("-", " ").lower().split())
    exact: dict[str, list[str]] = {}     # code -> spellings of the IAU name
    loose: dict[str, list[str]] = {}     # code -> spellings without a month/branch
    for code, info in showers.items():
        if not info.get("name") or code in ("SPO", "ANT"):
            continue
        vs = variants(info["name"])
        n = " ".join(info["name"].split())
        exact[code] = [v for v in vs if norm(v) == norm(n)]
        loose[code] = [v for v in vs if norm(v) != norm(n)]
    found: dict[str, str] = {}
    with requests.Session() as s:
        titles = list(dict.fromkeys(t for vs in (*exact.values(), *loose.values()) for t in vs))
        for i in range(0, len(titles), 50):
            found.update(lookup(titles[i:i + 50], s))
            time.sleep(0.5)

    # 1. The IAU name itself (any spelling) finds the article: that is it.
    out: dict[str, str] = {}
    for code, vs in exact.items():
        hit = next((found[v] for v in vs if v in found), None)
        if hit and hit not in GENERAL:
            out[code] = hit
    taken = set(out.values())
    # 2. Without the month or branch, many minor showers land on a famous
    #    one's article ("December Leonids" on Leonids). Such a match counts
    #    only when no shower has the article by its own name, and then
    #    (a) for the shower whose full name is the article's title, else
    #    (b) for the showers with an IMO rate (the established ones: both
    #        Taurid branches on "Taurids"), else (c) for a lone claimant.
    claims: dict[str, list[str]] = {}
    for code, vs in loose.items():
        if code in out:
            continue
        hit = next((found[v] for v in vs if v in found), None)
        if hit and hit not in GENERAL and hit not in taken:
            claims.setdefault(hit, []).append(code)
    for title, codes in claims.items():
        own = [c for c in codes if norm(showers[c]["name"]) == norm(title)]
        rated = [c for c in codes if showers[c].get("zhr") is not None]
        winners = own or rated or (codes if len(codes) == 1 else [])
        if not winners:
            logger.info("ambiguous %r for %s: dropped", title, codes)
        for c in winners:
            out[c] = title
        for c in set(codes) - set(winners):
            logger.info("%s (%s) does not get %r", c, showers[c]["name"], title)
    # 3. The article must be about a shower, not only file it under one
    #    ("Camelopardalids" redirects to the comet 209P/LINEAR's article).
    with requests.Session() as s:
        arts = sorted(set(out.values()))
        intro: dict[str, str] = {}
        for i in range(0, len(arts), 20):
            r = s.get(API, params={"action": "query", "format": "json", "prop": "extracts", "exintro": 1,
                                   "explaintext": 1, "exsentences": 2, "titles": "|".join(arts[i:i + 20])},
                      headers=HEADERS, timeout=30)
            r.raise_for_status()
            for page in r.json()["query"]["pages"].values():
                intro[page["title"]] = page.get("extract", "").lower()
    for code, title in list(out.items()):
        if "meteor shower" not in intro.get(title, "") and "meteor stream" not in intro.get(title, ""):
            logger.info("%s: %r is not a shower article, dropped", code, title)
            out.pop(code)
    a.output.write_text(json.dumps(dict(sorted(out.items())), indent=1, ensure_ascii=False) + "\n")
    logger.info("%d of %d showers have an article -> %s", len(out), len(showers), a.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
