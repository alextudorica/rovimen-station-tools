"""Final Data Products: which nightly plots are kept forever, and where.

Stations upload two per-night images into archive subdirs that the box prune
empties on a schedule: the colour meteor stack rides in ``stacks/`` (whole dir
removed after 60 days, non-detection stacks after 30) and the night composite
rides in ``timelapse/`` (removed after 14 days with the timelapse MP4). Both are
final products the Final Data Products tab shows, so ``box_archive_prune.py``
copies them into ``<cam>/<date>/fdp/`` before it deletes their source dir, and
nothing prunes ``fdp/``. The RMS plots (``rms/``) and spectro products
(``spectro/``) already live in never-pruned subdirs and need no copy.

Readers resolve ``fdp/`` first and fall back to the upload subdir, so a night
the prune has not reached yet is served from where it was uploaded.

Dependency-free on purpose: ``tools/box_archive_prune.py`` imports it on the
VPS, where it is deployed flat next to the dashboard modules.
"""

from __future__ import annotations

from pathlib import Path

# Archive subdir that holds the permanent copies. Listed in no prune rule.
FDP_SUBDIR = "fdp"

# Synthetic filenames the plot-image route resolves to a camera/night product,
# so the browser can ask for "this night's X" without knowing the real name.
# COLOR_METEOR_STACK_FILENAME matches route_helpers and the static JS.
COLOR_METEOR_STACK_FILENAME = "__color_meteor_stack__.webp"
NIGHT_COMPOSITE_FILENAME = "__night_composite__.webp"

# kind -> (filename suffix after "<cam>_<date>", subdir the station uploads to)
FDP_PRODUCTS: dict[str, tuple[str, str]] = {
    "color_meteor_stack": ("_color_meteor_stack.webp", "stacks"),
    "night_composite": ("_night_stack.webp", "timelapse"),
}

_SYNTHETIC_TO_KIND: dict[str, str] = {
    COLOR_METEOR_STACK_FILENAME: "color_meteor_stack",
    NIGHT_COMPOSITE_FILENAME: "night_composite",
}


def product_filename(kind: str, cam: str, date: str) -> str:
    """Real archive filename of *kind* for one camera night."""
    suffix, _subdir = FDP_PRODUCTS[kind]
    return f"{cam}_{date}{suffix}"


def products_in_subdir(subdir: str) -> list[str]:
    """Kinds whose upload copy lives in *subdir* (``stacks`` / ``timelapse``)."""
    return [k for k, (_s, sub) in FDP_PRODUCTS.items() if sub == subdir]


def kind_for_synthetic(filename: str) -> str | None:
    """Product kind for a synthetic plot-image filename, else None."""
    return _SYNTHETIC_TO_KIND.get(filename)


def archive_candidates(archive_root: Path, kind: str, cam: str, date: str) -> list[Path]:
    """Where *kind* may sit on the archive for one night, in lookup order:
    the permanent ``fdp/`` copy, then the upload subdir it was pushed into."""
    _suffix, subdir = FDP_PRODUCTS[kind]
    name = product_filename(kind, cam, date)
    night = archive_root / cam / date
    return [night / FDP_SUBDIR / name, night / subdir / name]
