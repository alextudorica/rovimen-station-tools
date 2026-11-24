"""Overlay text cannot add a filter to the ffmpeg graph.

``build_drawtext_annotations`` used to write ``coords``, ``network`` and the
station id into ``drawtext=...:text=<value>`` raw, so a comma in ``coords``
ended the drawtext and started a filter of the caller's choosing, e.g.
``drawtext=textfile=~/.ssh/id_rsa``, whose text the station API's overlay
preview then returned as a PNG. Every literal value now goes through
``overlay.escape_drawtext``.

ffmpeg reads a drawtext value three times: the filtergraph parser (splits
filters at ``,`` and ``;``), the filter's option parser (splits options at
``:``) and drawtext's own text expansion (``\\`` and ``%``). The model below
follows libavutil's ``av_get_token`` for the first two; the ffmpeg tests
check the same thing against the real binary, and that today's station
configs render pixel for pixel as they did before the escaping.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import overlay

REPO = Path(__file__).resolve().parents[1]
FONT = REPO / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
STATION_CONFIGS = sorted((REPO / "station_configs").glob("*/config.json"))

HOSTILE = [
    "45.12N, 25.46E",
    "x,drawtext=textfile=/nonexistent/secret",
    "x',drawtext=textfile=/nonexistent/secret:x='",
    "a:b",
    "it's",
    "a\\b",
    "\\'",
    "100%",
    "%{e:1+1}",
    "x;y[z]",
    "'",
]

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not FONT.is_file(),
    reason="ffmpeg or the bundled font is missing",
)

_WS = " \n\t\r"


def _av_get_token(buf: str, term: str) -> tuple[str, str]:
    """libavutil ``av_get_token``: the unescaped token and the rest of *buf*
    from its terminator on."""
    i, n = 0, len(buf)
    while i < n and buf[i] in _WS:
        i += 1
    out: list[str] = []
    end = 0
    while i < n and buf[i] not in term:
        c = buf[i]
        i += 1
        if c == "\\" and i < n:
            out.append(buf[i])
            i += 1
            end = len(out)
        elif c == "'":
            while i < n and buf[i] != "'":
                out.append(buf[i])
                i += 1
            if i < n:
                i += 1
                end = len(out)
        else:
            out.append(c)
    while len(out) > end and out[-1] in _WS:
        out.pop()
    return "".join(out), buf[i:]


def _parse_chain(chain: str) -> list[tuple[str, dict[str, str]]]:
    """A ``-vf`` chain as ffmpeg's graph and option parsers see it."""
    filters: list[tuple[str, dict[str, str]]] = []
    rest = chain
    while True:
        name, rest = _av_get_token(rest, "=,;[")
        opts: dict[str, str] = {}
        if rest.startswith("="):
            args, rest = _av_get_token(rest[1:], "[],;")
            while args:
                key, args = _av_get_token(args, "=:")
                assert args.startswith("="), f"option without a value in {name}: {key!r}"
                value, args = _av_get_token(args[1:], ":")
                opts[key] = value
                args = args[1:] if args.startswith(":") else args
        filters.append((name, opts))
        if not rest:
            return filters
        assert rest[0] in ",;", rest
        rest = rest[1:]


def _expand(text: str) -> str:
    """drawtext's text expansion for a value with no ``%{...}`` in it."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        assert text[i] != "%", f"unescaped % would expand: {text!r}"
        out.append(text[i])
        i += 1
    return "".join(out)


def _cfg(**overrides) -> dict:
    cfg = {"font": str(FONT), "font_size": 19, "style": "cinema",
           "coords": "45.12N  25.46E", "network": "ROVIMEN"}
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# escape_drawtext
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", HOSTILE + ["", "51.62N  13.70E", "SHW PER"])
def test_escaped_text_is_one_option_of_one_filter(text):
    chain = f"drawtext=fontfile=/f.ttf:text={overlay.escape_drawtext(text)}:x=1,null"
    filters = _parse_chain(chain)
    assert [name for name, _ in filters] == ["drawtext", "null"]
    opts = filters[0][1]
    assert set(opts) == {"fontfile", "text", "x"}
    assert _expand(opts["text"]) == text


def test_plain_text_keeps_the_old_quoted_form():
    """Values without a quote, backslash or percent escape exactly as before,
    so the details rows burned into clips are unchanged."""
    assert overlay.escape_drawtext("a:b") == r"'a\:b'"
    assert overlay.escape_drawtext("SHW PER") == "'SHW PER'"


# ---------------------------------------------------------------------------
# build_drawtext_annotations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["cinema", "standard"])
@pytest.mark.parametrize("field", ["coords", "network", "station_id"])
@pytest.mark.parametrize("value", HOSTILE)
def test_config_text_cannot_add_a_filter(style, field, value):
    cfg = _cfg(style=style)
    station_id = "RO000H"
    if field == "station_id":
        station_id = value
    else:
        cfg[field] = value
    filters, _ = overlay.build_drawtext_annotations(cfg, station_id, {"az": 1, "alt": 2}, 1_700_000_000)
    parsed = _parse_chain(",".join(filters))
    assert len(parsed) == len(filters)
    assert all(name == "drawtext" for name, _ in parsed)
    assert not any("textfile" in opts for _, opts in parsed)
    drawn = [_expand(opts["text"]) for _, opts in parsed if "%{pts" not in opts["text"]]
    assert value in drawn


@needs_ffmpeg
@pytest.mark.timeout(300)
@pytest.mark.parametrize("value", [
    "x,drawtext=textfile=/nonexistent/secret",
    "x',drawtext=textfile=/nonexistent/secret:x='",
])
def test_ffmpeg_draws_an_injection_attempt_as_text(value):
    """An injected ``textfile=`` filter would fail to open its file and fail
    the run; escaped, the attempt is drawn as text and ffmpeg succeeds."""
    filters, bar_h = overlay.build_drawtext_annotations(
        _cfg(coords=value), "RO000H", {}, 1_700_000_000)
    vf = ",".join([f"pad=iw:ih+{bar_h}:0:0:black", *filters])
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x111111:size=640x360:rate=1:duration=1",
         "-vf", vf, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-400:]


def _render(vf: str, tmp_path: Path) -> bytes:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x111111:size=1280x720:rate=1:duration=1",
         "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-400:]
    return r.stdout


@needs_ffmpeg
@pytest.mark.timeout(300)
@pytest.mark.parametrize("text", HOSTILE)
def test_ffmpeg_draws_the_literal_text(text, tmp_path):
    """The escaped value renders exactly like the same text read from a file
    with expansion off."""
    (tmp_path / "ref.txt").write_text(text)
    common = f"fontfile={FONT}:fontsize=24:fontcolor=white:x=10:y=10"
    escaped = _render(f"drawtext={common}:text={overlay.escape_drawtext(text)}", tmp_path)
    reference = _render(f"drawtext={common}:textfile=ref.txt:expansion=none", tmp_path)
    assert escaped == reference


def _unescaped(filters: list[str]) -> list[str]:
    """The filter strings as they were built before the escaping: the text
    value unquoted. Exact only for values with no quote, colon, backslash or
    percent, which the test asserts first."""
    return [re.sub(r":text='([^'\\:%]*)'", r":text=\1", f) for f in filters]


def _station_overlay_cases() -> list[tuple[str, dict, str, dict]]:
    cases = []
    for path in STATION_CONFIGS:
        cfg = json.loads(path.read_text())
        ov = cfg.get("overlay") or {}
        stations = cfg.get("stations") or {}
        if not ov or not stations:
            continue
        cam = next(iter(stations))
        scfg = stations[cam] if isinstance(stations[cam], dict) else {}
        station_cfg = {k: scfg[k] for k in ("az", "alt") if scfg.get(k) is not None}
        cases.append((path.parent.name, {**ov, "font": str(FONT)}, cam, station_cfg))
    return cases


def test_station_configs_are_present():
    if not STATION_CONFIGS:
        pytest.skip("per-station configs are not part of the public repository")
    assert len(_station_overlay_cases()) >= 10


@needs_ffmpeg
@pytest.mark.timeout(300)
@pytest.mark.parametrize("host,ov,cam,station_cfg", _station_overlay_cases(),
                         ids=[c[0] for c in _station_overlay_cases()])
def test_todays_station_overlays_render_as_before(host, ov, cam, station_cfg, tmp_path):
    filters, bar_h = overlay.build_drawtext_annotations(ov, cam, station_cfg, 1_700_000_000)
    texts = [ov.get("coords", ""), ov.get("network", "ROVIMEN"), cam]
    for text in texts:
        assert not set(str(text)) & set("'\\:%"), f"{host}: {text!r} needs the new escaping"
    legacy = _unescaped(filters)
    assert legacy != filters
    pad = [f"pad=iw:ih+{bar_h}:0:0:black"] if bar_h else []
    assert _render(",".join(pad + filters), tmp_path) == _render(",".join(pad + legacy), tmp_path)
