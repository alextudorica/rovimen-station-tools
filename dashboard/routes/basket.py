"""Download-basket routes — per-user clip basket with one-zip checkout.

Any signed-in account can collect clips from anywhere on the site (every
surface that opens the shared VideoModal gets an add-to-basket toggle) and
then download the whole set as a single zip. Clips and their stack images
sit flat at the zip root — names embed camera+date+time, so no folder
nesting is needed — with the faststart ``.mp4`` preferred over the ``.mkv``
original, plus a ``manifest.json`` describing what was (and wasn't)
included. Entries added with a trim window (the modal's handle selection)
ship as an ffmpeg cut of just those seconds instead of the full clip; the
range-suffixed arcname (``..._t12.3-15.4s.mp4``) can never collide with
the full clip's name.

Checkout serves straight from the storagebox archive — the same
archive-first model as clip playback: locked clips land there within
minutes of detection, so no station round-trips are needed. A basket entry
whose clip has not reached the archive yet stays in the basket and is
reported as ``missing`` in the manifest instead of failing the download.

Admins can add ``?fireball=1`` to checkout: for every basket item the
owning station is asked for a fireball analysis pack (platepars,
FTPdetectinfo, CALSTARS, FF/FR frames around the event — see
station_api.py api_fireball_pack) whose files land in the zip under
``fireball_pack/<camera>_<date>[_<time>]/``. This is the one basket path
that DOES hit stations live; an unreachable station degrades to a
manifest note.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, session, stream_with_context

import basket_store
import cache_store
from auth import require_auth

logger = logging.getLogger(__name__)

_CLIP_EXT_RE = re.compile(r"\.(mkv|mp4)$", re.IGNORECASE)
_STACK_EXT_RE = re.compile(r"\.(webp|jpg|jpeg|png)$", re.IGNORECASE)
_ENTRY_ID_RE = re.compile(r"^bk_[0-9a-f]{12}$")

# Item cap per basket; guards the store file and the checkout fan-out.
_MAX_ITEMS = int(os.environ.get("ROVIMEN_BASKET_MAX_ITEMS", "200"))
# Aggregate zip cap. Unlike /api/batch-download full clips are never copied
# to /tmp — they stream straight from the archive mount into the zip (only
# trimmed cuts, a few MB each, land in the checkout tempdir first) — so this
# caps bandwidth/wall-time per request, not disk. Faststart MP4 clips run a
# few MB each; 4 GiB comfortably covers a 200-item basket of stitched clips.
_MAX_TOTAL_BYTES = int(
    os.environ.get("ROVIMEN_BASKET_MAX_BYTES", str(4 * 1024 * 1024 * 1024))
)

# First HHMMSS after the date token in a clip name — the event time the
# station needs to pick FF/FR frames. Stitched boundary clips
# (<cam>_<date>_<Tstart>_<Tend>_color.mkv) yield Tstart.
_CLIP_TIME_RE = re.compile(r"_\d{8}_(\d{6})")

# Zip members from stations are re-hosted under our own arcnames; reject
# anything that could escape the target prefix.
_PACK_MEMBER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")

# Station fireball packs fetched at once per checkout. Each comes from a
# different station, so this bounds VPS threads and bandwidth, not any one
# station's load.
_PACK_FETCH_WORKERS = 6

# Wall-time budget for one trim cut at checkout; basket clips are a few
# seconds to ~20 s, so a healthy re-encode finishes in single-digit seconds.
# Cuts run inside the zip stream now; one longer than nginx's 30 s gap
# between bytes would cut the download off, so fail it (full clip instead).
_CUT_TIMEOUT_S = 25


def _cut_clip(src: Path, start: float, duration: float, dest: Path) -> str | None:
    """Re-encode the [start, start+duration) window of *src* into *dest*.

    ``-ss`` before ``-i`` plus a full re-encode gives frame-accurate-enough
    cuts (stream-copy would snap to the previous keyframe, seconds away on
    these long-GOP clips); faststart so the cut plays anywhere the archived
    ``.mp4`` siblings do. Returns an error string on failure, None on
    success. Module-level so tests can patch it."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.1f}", "-t", f"{duration:.1f}", "-i", str(src),
        "-fps_mode", "passthrough",  # MKV reports 1000 fps; see _build_crop_cmd
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_CUT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return str(exc)[:200]
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        err = proc.stderr.decode(errors="replace").strip()[:200]
        return err or f"ffmpeg exit {proc.returncode}"
    return None


def _lazy_cut(clip: Path, trim_start: float, trim_end: float, cut_name: str,
              row: dict, tmpdir: str):
    """A zip entry that cuts the trimmed window when the stream reaches it.
    A failed cut ships the full clip and says so in the manifest row."""
    def _produce() -> list[tuple[str, Path]]:
        cut_path = Path(tmpdir) / cut_name
        err = _cut_clip(clip, trim_start, trim_end - trim_start, cut_path)
        if err is None:
            row["clip"] = cut_name
            row["trim"]["status"] = "applied"
            return [(cut_name, cut_path)]
        logger.warning("basket trim cut failed for %s: %s", clip.name, err)
        row["trim"]["status"] = f"cut failed ({err}); full clip included"
        return [(clip.name, clip)]
    return _produce


def _download_pack(config, tunnels, camera: str, date_compact: str,
                   time_s: str | None, dest: Path) -> str | None:
    """Fetch one station fireball pack into *dest*. Returns an error string
    on failure, None on success. Module-level so tests can patch it."""
    from auth import _host_keys_for_camera
    from station_client import _session_for_url, station_url

    host_keys = _host_keys_for_camera(config, camera)
    if not host_keys:
        return "no station owns this camera"
    path = f"/api/fireball_pack/{camera}/{date_compact}"
    if time_s:
        path += f"?time={time_s}"
    last_err = "station unreachable"
    for host_key in host_keys:
        try:
            url = station_url(config, tunnels, host_key, path)
            resp = _session_for_url(url).get(url, timeout=(10, 90), stream=True)
            if resp.status_code == 404:
                return "no night data on station"
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(65536):
                    fh.write(chunk)
            return None
        except Exception as exc:  # tunnel down, HTTP error, short read
            last_err = str(exc)[:200]
            logger.warning("fireball pack fetch failed for %s via %s: %s",
                           camera, host_key, exc)
    return last_err


def register_basket_routes(app: Flask, config=None, tunnels=None) -> None:
    # Lazy-import siblings that live in the same dashboard package dir.
    import public_api as _pub
    from routes.media import _stream_zip_from_files

    def _bad(msg: str, code: int = 400):
        # JSON (not Flask's HTML abort page) so the frontend toast can show
        # the actual reason instead of a bare "HTTP 400".
        return jsonify({"error": msg}), code

    def _norm_date_iso(raw) -> str | None:
        """Normalise YYYYMMDD / YYYY-MM-DD to canonical YYYY-MM-DD.

        Clip surfaces disagree on the date shape (same split as the
        hall-of-fame add route): Highlights sends ISO, the Video DB sends
        compact. Entries are stored ISO; the archive tree uses compact.
        """
        raw = str(raw or "").strip()
        if re.fullmatch(r"\d{8}", raw):
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        if _pub._DATE_ISO_RE.match(raw):
            return raw
        return None

    def _archive_file(camera: str, date_compact: str, name: str) -> Path | None:
        """First existing archive file for *name* under this night.

        Mirrors routes.archive._archive_clip_path plus the ``stacks/``
        subdir some older nights use, but resolves ARCHIVE_PATH through the
        cache_store module at call time so tests can patch one place.
        """
        base = cache_store.ARCHIVE_PATH / camera / date_compact
        for subdir in ("meteors", "", "stacks"):
            p = (base / subdir / name) if subdir else (base / name)
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return p
            except OSError:
                continue
        return None

    # ── Read ─────────────────────────────────────────────────────────────

    @app.route("/api/basket")
    @require_auth
    def basket_list():
        items = basket_store.list_for(session["user"])
        return jsonify({"items": items, "count": len(items)})

    # ── Mutations ────────────────────────────────────────────────────────

    @app.route("/api/basket", methods=["POST"])
    @require_auth
    def basket_add():
        payload = request.get_json(silent=True) or {}
        camera = str(payload.get("camera", "")).strip().upper()
        filename = str(payload.get("filename", "")).strip()
        date_iso = _norm_date_iso(payload.get("date"))

        if not _pub._CAMERA_RE.match(camera):
            return _bad("invalid camera code")
        if date_iso is None:
            return _bad("invalid date; expected YYYY-MM-DD or YYYYMMDD")
        if not (_pub._FILENAME_RE.match(filename) and _CLIP_EXT_RE.search(filename)):
            return _bad("invalid clip filename")

        # Stack hint is optional and best-effort: checkout derives the name
        # from the clip when the hint is absent or malformed.
        stack = str(payload.get("stack", "") or "").strip() or None
        if stack and not (_pub._FILENAME_RE.match(stack)
                          and _STACK_EXT_RE.search(stack)):
            stack = None

        # Optional trim window (seconds) from the modal's handle selection.
        # The frontend only sends both bounds together, and only for a
        # proper sub-range — an untrimmed add carries no trim fields, so
        # its stored entry keeps the exact shape it always had.
        trim_start = payload.get("trim_start")
        trim_end = payload.get("trim_end")
        has_trim = trim_start is not None or trim_end is not None
        if has_trim:
            try:
                trim_start = round(float(trim_start), 1)
                trim_end = round(float(trim_end), 1)
            except (TypeError, ValueError):
                return _bad("invalid trim window")
            if not 0 <= trim_start < trim_end:
                return _bad("invalid trim window; need 0 <= trim_start < trim_end")

        entry = {
            "id":            f"bk_{uuid.uuid4().hex[:12]}",
            "camera":        camera,
            "date":          date_iso,
            "filename":      filename,
            "stack":         stack,
            "station_label": (str(payload.get("station_label")).strip()[:64]
                              if payload.get("station_label") else None),
            "added_at":      datetime.now(timezone.utc).isoformat(),
        }
        if has_trim:
            entry["trim_start"] = trim_start
            entry["trim_end"] = trim_end
        try:
            basket_store.add(session["user"], entry, max_items=_MAX_ITEMS)
        except basket_store.DuplicateClipError:
            return _bad("clip is already in the basket", 409)
        except basket_store.BasketFullError:
            return _bad(f"basket is full ({_MAX_ITEMS} items)", 409)
        return jsonify({"item": entry}), 201

    @app.route("/api/basket/<entry_id>", methods=["DELETE"])
    @require_auth
    def basket_remove(entry_id: str):
        if not _ENTRY_ID_RE.match(entry_id):
            return _bad("invalid basket entry id")
        if not basket_store.remove(session["user"], entry_id):
            return _bad("entry not found", 404)
        return jsonify({"ok": True})

    @app.route("/api/basket/clear", methods=["POST"])
    @require_auth
    def basket_clear():
        return jsonify({"ok": True,
                        "removed": basket_store.clear(session["user"])})

    # ── Checkout ─────────────────────────────────────────────────────────

    @app.route("/api/basket/checkout")
    @require_auth
    def basket_checkout():
        """Stream the whole basket as one zip: clips + stacks + manifest.

        GET so the frontend can hand the URL straight to the browser's
        downloader (session cookie rides along). Streams via the shared
        stored-zip generator, so the archive never sits in RAM.
        """
        items = basket_store.list_for(session["user"])
        if not items:
            return _bad("basket is empty")

        fireball = request.args.get("fireball") == "1"
        if fireball and session.get("role") != "admin":
            return _bad("fireball packs are admin-only", 403)

        entries: list[tuple[str, Path]] = []
        seen_arcnames: set[str] = set()
        manifest_items: list[dict] = []
        total_bytes = 0

        def _push(arcname: str, path: Path, into: list | None = None) -> None:
            # Two basket entries can share a stack (or a re-added clip after
            # a corrupt-store recovery could repeat); a duplicate arcname
            # would make the zip ambiguous, so first one wins.
            nonlocal total_bytes
            if arcname in seen_arcnames:
                return
            seen_arcnames.add(arcname)
            total_bytes += path.stat().st_size
            (entries if into is None else into).append((arcname, path))

        def _fetch_and_extract(download, camera: str, date_compact: str,
                               time_s: str | None, workdir: Path):
            """One station pack, downloaded and unpacked under *workdir*.

            Returns a list of (member, path) on success, else a manifest
            note string. Member names are filtered (no traversal, no
            absolute paths) before we re-host them under our own arcnames.
            Runs on a prefetch thread, so *download* is resolved by the
            caller rather than looked up here.
            """
            workdir.mkdir(parents=True, exist_ok=True)
            pack_zip = workdir / "pack.zip"
            err = download(config, tunnels, camera, date_compact,
                           time_s, pack_zip)
            if err:
                return f"unavailable: {err}"
            if not workdir.is_dir():
                return "unavailable: checkout closed"  # client gave up
            out: list[tuple[str, Path]] = []
            try:
                with zipfile.ZipFile(pack_zip) as zf:
                    for zi in zf.infolist():
                        if (zi.is_dir() or ".." in zi.filename
                                or not _PACK_MEMBER_RE.match(zi.filename)):
                            continue
                        target = workdir / "x" / zi.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(zi) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        out.append((zi.filename, target))
            except zipfile.BadZipFile:
                return "unavailable: corrupt pack from station"
            return out or "unavailable: empty pack"

        def _pack_key(it: dict) -> tuple[str, str, str | None]:
            tm = _CLIP_TIME_RE.search(it["filename"])
            return (it["camera"], it["date"].replace("-", ""),
                    tm.group(1) if tm else None)

        def _start_pack_prefetch(tmpdir: str) -> tuple[ThreadPoolExecutor, dict]:
            """Fetch every distinct (camera, night, event-time) pack in
            parallel, starting now, while the clips stream ahead of them.

            Fetched one after another inside the stream, a basket's packs
            added up to minutes with no byte sent, and nginx's 30 s
            read timeout cut the download (11 packs, ~50 s, 2026-10-16).
            In parallel, the longest silence is the slowest single pack
            minus the time the clips took to stream.
            """
            download = _download_pack
            pool = ThreadPoolExecutor(max_workers=_PACK_FETCH_WORKERS,
                                      thread_name_prefix="basket-pack")
            futures: dict[tuple, Future] = {}
            for idx, it in enumerate(items):
                key = _pack_key(it)
                if key not in futures:
                    futures[key] = pool.submit(
                        _fetch_and_extract, download, *key,
                        Path(tmpdir) / f"pack_{idx}")
            return pool, futures

        def _pack_entry(idx: int, futures: dict,
                        outcomes: dict) -> list[tuple[str, Path]]:
            """Splice basket item *idx*'s station pack into the zip under
            fireball_pack/<camera>_<date>[_<time>]/. Per-event subfolders
            are load-bearing: every pack repeats member names (SESS/...,
            frames/...), and the first-wins arcname dedup would silently
            drop the second event's files in a shared folder. Items that
            share a pack reuse its outcome; a dead station degrades to a
            manifest note instead of failing the whole checkout."""
            it, row = items[idx], manifest_items[idx]
            key = _pack_key(it)
            if key not in outcomes:
                got = futures[key].result()
                if not isinstance(got, str) and total_bytes > _MAX_TOTAL_BYTES:
                    # Clips already passed the cap check; packs are
                    # best-effort extras and must not push past it.
                    got = "skipped: size cap reached"
                outcomes[key] = got
            got = outcomes[key]
            if isinstance(got, str):
                row["fireball_pack"] = got
                return []
            camera, date_compact, time_s = key
            event_dir = f"{camera}_{date_compact}" + (f"_{time_s}" if time_s else "")
            out: list[tuple[str, Path]] = []
            for member, path in got:
                _push(f"fireball_pack/{event_dir}/{member}", path, into=out)
            row["fireball_pack"] = "included"
            return out

        # The tempdir exists for the whole build (not just fireball packs):
        # trimmed entries re-encode their cut window into it. Cuts, packs and
        # the manifest are lazy zip entries that run when the stream reaches
        # them, so the first byte no longer waits for every ffmpeg cut
        # (several of them outlasted nginx's 30 s). The streaming generator
        # owns the cleanup on success; every other exit removes it here.
        tmpdir = tempfile.mkdtemp(prefix="basket_")
        pool: ThreadPoolExecutor | None = None
        try:
            for it in items:
                camera, date_iso, filename = it["camera"], it["date"], it["filename"]
                date_compact = date_iso.replace("-", "")
                stem, _ext = filename.rsplit(".", 1)

                # Prefer the faststart .mp4 (plays everywhere, smaller) over
                # the .mkv original the basket usually records — the pusher
                # uploads both siblings, so the .mp4 is normally present once
                # the clip has archived; fall back to the .mkv if the remux
                # is missing.
                clip = (_archive_file(camera, date_compact, stem + ".mp4")
                        or _archive_file(camera, date_compact, stem + ".mkv"))

                stack_name = it.get("stack") or _pub._derive_stack_name(filename)
                stack = (_archive_file(camera, date_compact, stack_name)
                         if stack_name else None)

                row = {
                    "camera":        camera,
                    "date":          date_iso,
                    "filename":      filename,
                    "station_label": it.get("station_label"),
                    "added_at":      it.get("added_at"),
                    "clip":          clip.name if clip else None,
                    "stack":         stack.name if stack else None,
                    "status":        "included" if clip else "missing",
                }

                # Trimmed entries ship an ffmpeg cut of just the selected
                # window; the range suffix in the arcname means the cut can
                # never collide with the full clip under the first-wins
                # dedup. A failed cut degrades to the full clip with a
                # manifest note instead of failing the whole checkout.
                trim_start, trim_end = it.get("trim_start"), it.get("trim_end")
                manifest_items.append(row)
                if (clip is not None and trim_start is not None
                        and trim_end is not None):
                    row["trim"] = {"start": trim_start, "end": trim_end}
                    cut_name = (clip.name.rsplit(".", 1)[0]
                                + f"_t{trim_start:.1f}-{trim_end:.1f}s.mp4")
                    if cut_name not in seen_arcnames:
                        seen_arcnames.add(cut_name)
                        # Size cap: the full clip bounds its cut.
                        total_bytes += clip.stat().st_size
                        entries.append(_lazy_cut(clip, trim_start, trim_end,
                                                 cut_name, row, tmpdir))
                elif clip is not None:
                    # Flat zip root: clip names embed camera+date+time, so
                    # naming alone distinguishes them.
                    _push(clip.name, clip)
                if stack is not None:
                    _push(stack.name, stack)

            if not entries:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return _bad(
                    "none of the basket clips have reached the archive yet",
                    404)
            if total_bytes > _MAX_TOTAL_BYTES:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return jsonify({
                    "error":       "basket too large for one download",
                    "total_bytes": total_bytes,
                    "max_bytes":   _MAX_TOTAL_BYTES,
                }), 413

            if fireball:
                pool, futures = _start_pack_prefetch(tmpdir)
                outcomes: dict = {}
                # One lazy entry per item, so each pack streams as soon as
                # its own fetch is done instead of after all of them.
                for idx in range(len(items)):
                    entries.append(functools.partial(
                        _pack_entry, idx, futures, outcomes))

            def _manifest_entry() -> list[tuple[str, Path]]:
                # Last, so it reports how every cut and pack actually went.
                manifest_path = Path(tmpdir) / "manifest.json"
                manifest_path.write_text(json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "item_count":   len(items),
                    "items":        manifest_items,
                }, indent=2))
                return [("manifest.json", manifest_path)]
            entries.append(_manifest_entry)
        except Exception:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

        def _stream():
            try:
                yield from _stream_zip_from_files(entries, cleanup_dir=tmpdir)
            finally:
                # A client that gives up mid-download must not leave queued
                # station fetches running; one already in flight finds its
                # tempdir gone and is dropped.
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Response(
            stream_with_context(_stream()),
            mimetype="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="rovimen_basket_{stamp}.zip"'},
        )
