"""tools/solve_spectro_pointing.py runs as a uv script with its own dependencies.

It needs scipy, which the project does not carry (not in uv.lock), so a plain
`uv run` in the project env failed on the import. The PEP 723 block must name
every third-party module the script imports, eagerly or inside a function.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "solve_spectro_pointing.py"
DIST_OF = {"PIL": "pillow"}


def _inline_metadata(source: str) -> dict:
    m = re.search(r"^# /// script\n((?:#.*\n)+?)^# ///$", source, re.MULTILINE)
    assert m, "no PEP 723 '# /// script' block"
    return tomllib.loads("".join(line[2:] if line.startswith("# ") else line[1:]
                                 for line in m.group(1).splitlines(keepends=True)))


def _third_party_imports(source: str) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names}


def test_every_third_party_import_is_a_declared_dependency() -> None:
    source = SCRIPT.read_text()
    declared = {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower()
                for d in _inline_metadata(source)["dependencies"]}
    needed = {DIST_OF.get(m, m).lower() for m in _third_party_imports(source)}
    assert needed <= declared, f"undeclared: {sorted(needed - declared)}"
    assert "scipy" in declared


def test_usage_runs_it_as_a_script() -> None:
    assert "uv run --script tools/solve_spectro_pointing.py" in SCRIPT.read_text()
