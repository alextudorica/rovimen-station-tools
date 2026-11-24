"""Each overview page loads only its own page module, and every inline handler
a page can run names a function that a module on that page puts on window.

``overview.html`` renders two pages: home ("/") and the Network overview
("/network"). Home used to download and run the Network overview's whole
module (map, live view, GMN orbit layer, status table, about 165 kB) for its
counts and plots. Now home loads ``dashboard-home.js`` and ``/network`` loads
``dashboard-overview.js``. An inline ``on*=`` handler resolves its function
through window only when it runs, so a name left in the other page's module
would fail only on a click; these tests read the handlers from the rendered
page and from the HTML its modules generate, and look each one up. The
station page is checked the same way: its System Admin tab moved from
``dashboard-station.js`` into ``dashboard-sysadmin.js``.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_public_page_toggles import _build_app, _logged_in  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "dashboard" / "static"

_MODULE_SCRIPT = re.compile(r'<script type="module" src="/static/([a-z0-9-]+\.js)')
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\b(?:src|type)=)[^>]*>(.*?)</script>", re.S)
_IMPORT = re.compile(r"""\bfrom\s+['"](?:\./|/static/)([a-z0-9-]+\.js)['"]""")
_WINDOW_SET = re.compile(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)")
_GLOBAL_FN = re.compile(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
_HANDLER = re.compile(r"""\son[a-z]+="([^"]*)\"""")
_TEMPLATE_EXPR = re.compile(r"\$\{[^}]*\}")
_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_NOT_A_GLOBAL = {"if", "for", "while", "switch", "return", "typeof", "function"}


def _page_modules(page_html: str) -> set[str]:
    """The page's module scripts plus everything they import, transitively."""
    todo = set(_MODULE_SCRIPT.findall(page_html))
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo |= set(_IMPORT.findall((STATIC / name).read_text()))
    return seen


def _handler_calls(text: str) -> set[str]:
    """Bare function names called from inline on*="..." attributes in text."""
    names: set[str] = set()
    for value in _HANDLER.findall(text):
        code = _TEMPLATE_EXPR.sub("x", html.unescape(value))
        names |= set(_CALL.findall(code)) - _NOT_A_GLOBAL
    return names


def _unresolved_handlers(page_html: str) -> dict[str, set[str]]:
    """Handler names, by where they are written, that nothing on the page defines."""
    modules = _page_modules(page_html)
    sources = {name: (STATIC / name).read_text() for name in modules}
    inline = "\n".join(_INLINE_SCRIPT.findall(page_html))
    defined = set(_GLOBAL_FN.findall(inline))
    for src in sources.values():
        defined |= set(_WINDOW_SET.findall(src))
    where = {"page": _handler_calls(page_html)}
    where |= {name: _handler_calls(src) for name, src in sources.items()}
    return {k: v - defined for k, v in where.items() if v - defined}


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    client = _logged_in(_build_app(["overview"]))
    paths = ("/", "/network", "/station/pub_station")
    return {path: client.get(path).get_data(as_text=True) for path in paths}


def test_home_loads_its_own_module_and_not_the_network_one(pages):
    home = pages["/"]
    assert "dashboard-home.js" in _MODULE_SCRIPT.findall(home)
    # Versioned like every other module, so the service worker's cache-first
    # copy of /static/ is replaced on each deploy.
    assert re.search(r'src="/static/dashboard-home\.js\?v=[^"]+"', home)
    modules = _page_modules(home)
    assert "dashboard-overview.js" not in modules
    assert "dashboard-map-point.js" not in modules
    assert "leaflet" not in home


def test_network_loads_the_network_module_and_not_the_home_one(pages):
    network = pages["/network"]
    assert "dashboard-overview.js" in _MODULE_SCRIPT.findall(network)
    assert "dashboard-home.js" not in _page_modules(network)
    assert "leaflet" in network


@pytest.mark.parametrize("path", ["/", "/network", "/station/pub_station"])
def test_every_inline_handler_on_the_page_is_defined_there(pages, path):
    assert _unresolved_handlers(pages[path]) == {}
