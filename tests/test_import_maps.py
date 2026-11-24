"""Every ES module the dashboard imports must be in every page's import map.

Static files are served with a 7-day public cache. A module reached through a
relative import (``from './dashboard-basket-add.js'``) and missing from the
page's import map loads unversioned, so browsers keep running the old copy
after a deploy: after #1389 the old basket script drew an unstyled "Basket"
label at the bottom of the page under the new stylesheet.
"""

import re
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent.parent / "dashboard"
IMPORTED = sorted({m for f in (DASH / "static").glob("*.js")
                   for m in re.findall(r"from '\./([a-z0-9-]+\.js)'", f.read_text())})
PAGES = sorted(t for t in (DASH / "templates").glob("*.html") if "importmap" in t.read_text())


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_import_map_versions_every_imported_module(page):
    text = page.read_text()
    missing = [m for m in IMPORTED if f'"/static/{m}"' not in text]
    assert not missing, f"{page.name} import map lacks {missing}"


def test_found_modules_and_pages():
    assert "dashboard-basket-add.js" in IMPORTED and len(PAGES) >= 10
