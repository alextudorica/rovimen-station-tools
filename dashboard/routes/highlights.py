"""Highlights API routes -- brightest events, fireball reports, aggregation and media serving."""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, abort, jsonify, request, send_file

import hidden_detections
from http_caching import _json_cached
from security import public_route

logger = logging.getLogger(__name__)


def register_highlights_routes(app: Flask, config, cache, tunnels) -> None:

    # Lazy-import siblings that live in the same dashboard package dir.
    import fireball_reports
    import gmn_data
    import highlight_events as hl_events
    from routes.detections import compute_detections_payload

    # ``limiter`` is stashed on the app by create_app().
    limiter = app._limiter  # type: ignore[attr-defined]

    def _abort_if_camera_not_public_for_anon(camera: str) -> None:
        """404 an anonymous request for a camera whose owning station is not
        ``public: true``.

        The highlights media routes key on camera code, not host_key, so this
        maps the code back to its owning station(s) and applies the same
        per-station ``public`` filter the rest of the anon surface enforces
        (PR #628). Logged-in accounts keep fleet-wide read access. Fails
        closed: an unknown camera code (no owner in the registry) is 404'd for
        anon, matching ``station_is_public_for_request``'s unknown-key rule."""
        from auth import is_anonymous, public_camera_codes

        if is_anonymous() and camera.upper() not in public_camera_codes(config):
            abort(404)

    # Endpoint-level cache for the (start, end) aggregation.  The per-date
    # detections payload is already cached, but this route additionally re-runs
    # the GMN event join, orbit tallies, coverage geometry and the top-10 sort
    # on every call.  Over a multi-week range that is slow enough to exhaust
    # nginx's upstream timeout and surface as a 503 -- and because gunicorn runs
    # one gthread worker, a slow recompute also stalls sibling requests (e.g.
    # thumbnails) sharing the thread pool.  Caching the assembled payload makes
    # repeat loads instant.  Threads share this dict, so guard it with a lock.
    _highlights_cache: "dict[tuple[str, str, bool], tuple[float, dict]]" = {}
    _highlights_lock = threading.Lock()
    _HIGHLIGHTS_MAX_ENTRIES = 64

    def _highlights_browser_max_age(end_str: str) -> int:
        """Past-only ranges are immutable (long cache); ranges touching today
        still accrue detections (short cache)."""
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return 1800 if end_str < today_iso else 120

    # ── Fireball research reports ────────────────────────────────────────

    def _station_for_camera(camera: str):
        from auth import camera_owner
        return camera_owner(config, camera)

    def _camera_visible(camera: str, anon: bool) -> bool:
        from auth import camera_owner_visible
        return camera_owner_visible(config, camera, anon)

    def _visible_reports(anon: bool) -> list:
        """Reports newest first that show at least one camera this visitor
        may see; the same rule the report page applies before a 404."""
        reports = sorted(fireball_reports.all_reports().values(),
                         key=lambda r: r.event_id, reverse=True)
        return [r for r in reports
                if any(_camera_visible(c, anon) for c in r.cameras)]

    @app.route("/api/highlights/reports")
    @public_route(page="highlights")
    def highlights_reports():
        """Every fireball research report with the clips and stacks of all
        its cameras, for the Highlights research gallery.

        An anonymous visitor gets only the cameras of ``public: true``
        stations (``/fireball/<id>`` applies the same rule), and the media
        URLs go through the highlights archive routes, which re-check it."""
        import public_api as _pub
        from auth import is_anonymous

        anon = is_anonymous()
        out = []
        for rep in _visible_reports(anon):
            event_time = (f"{rep.event_id[:4]}-{rep.event_id[4:6]}-{rep.event_id[6:8]}T"
                          f"{rep.event_id[9:11]}:{rep.event_id[11:13]}:{rep.event_id[13:15]}")
            witnesses = []
            for clip in rep.clips:
                if not _camera_visible(clip.camera, anon):
                    continue
                hk, st = _station_for_camera(clip.camera)
                stack = _pub._derive_stack_name(clip.filename)
                witnesses.append({
                    "camera":             clip.camera,
                    "host_key":           hk,
                    "station_label":      st.label if st else None,
                    "filename":           clip.filename,
                    "date":               clip.date,
                    "time_utc":           event_time,
                    "detection_offset_s": hl_events.detection_offset(clip.filename, event_time),
                    "clip_url":           f"/api/highlights/clip/{clip.camera}/{clip.date}/{clip.filename}",
                    "stack_url":          (f"/api/highlights/stack/{clip.camera}/{clip.date}/{stack}"
                                           if stack else None),
                })
            cameras = [c for c in rep.cameras if _camera_visible(c, anon)]
            places = []
            for c in cameras:
                _, st = _station_for_camera(c)
                if st is not None and st.label and st.label not in places:
                    places.append(st.label)
            # The first still figure stands in when the clips have no
            # archived stack (reports from before per-clip colour stacks).
            stills = list(dict.fromkeys(fig.poster or fig.file for sec in rep.sections
                                        for fig in sec.figures if fig.poster or not fig.is_video))
            cover = stills[0] if stills else None
            out.append({
                "event_id":   rep.event_id,
                "cover_url":  rep.figure_url(cover) if cover else None,
                # Every still figure of the report in reading order (video
                # figures by their poster): the card's slideshow.
                "slides":     [rep.figure_url(f) for f in stills],
                "url":        rep.url,
                "title":      rep.title,
                "status":     rep.status,
                "time_utc":   event_time,
                "local_time": rep.local_time,
                "analysed":   rep.analysed.isoformat(),
                "updated":    rep.updated.isoformat() if rep.updated else None,
                "summary":    rep.summary[0] if rep.summary else "",
                "facts":      [f.model_dump() for f in rep.facts[:6]],
                "cameras":    cameras,
                "places":     places,
                "witnesses":  witnesses,
            })
        return _json_cached({"reports": out}, max_age=60)

    # ── Highlights data endpoint ─────────────────────────────────────────

    @app.route("/api/highlights/data")
    @public_route(page="highlights")
    def highlights_data():
        """Aggregate detections + events + orbits across an arbitrary date range.

        Query params: start=YYYY-MM-DD, end=YYYY-MM-DD (inclusive).
        Returns stats summary + the brightest events of the range, each with
        every ROVIMEN camera that recorded it (``highlight_events``) and the
        fireball report that covers it, if any.

        Public under the "highlights" page toggle: the payload is aggregate
        network stats plus the brightest multi-station events (time, station
        label, camera code, shower, magnitude, duration, and clip/stack
        URLs). No station IPs, cam_ip, host paths,
        or per-user data — every field is already surfaced on the public
        media/detection surface, so anon exposure adds nothing sensitive.
        """
        import coverage as coverage_mod
        import platepar_store as platepar_store_mod
        from auth import is_anonymous, public_camera_codes

        start_str = request.args.get("start", "")
        end_str   = request.args.get("end", "")
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            end   = datetime.strptime(end_str,   "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "invalid dates; expected YYYY-MM-DD"}), 400
        if start > end:
            return jsonify({"error": "start must be <= end"}), 400
        if (end - start).days > 365:
            return jsonify({"error": "date range too large; maximum is 365 days"}), 400

        # Anonymous public visitors (highlights page toggled on) only ever see
        # detections from ``public: true`` stations — same opt-in set as the
        # /media/v1 and /api/stations surfaces (PR #628). Logged-in accounts
        # keep the full fleet. The filter runs on camera code because that's
        # what detections carry; anything not owned by a known public station
        # fails closed (dropped). The cache key includes ``anon`` so an admin
        # payload (full fleet) can never be served to an anonymous caller.
        anon = is_anonymous()
        _public_cams: set[str] | None = public_camera_codes(config) if anon else None

        def _visible_to_request(det: dict) -> bool:
            if not anon:
                return True
            cam = (det.get("cam") or det.get("camera") or "").upper()
            return bool(cam) and cam in (_public_cams or set())

        # Detections a user hid as false positives are left out of the
        # events (``hidden_detections``); a signed-in caller may ask for them
        # marked. The counts above the events (total detections, showers,
        # months) are network statistics and still include them.
        hidden_snap = hidden_detections.snapshot()
        show_hidden = hidden_detections.wants_hidden(request.args.get("show_hidden"), anon)
        ttl = _highlights_browser_max_age(end_str)
        cache_key = (start_str, end_str, anon, show_hidden, hidden_snap.version)
        now_mono = time.monotonic()
        with _highlights_lock:
            hit = _highlights_cache.get(cache_key)
            if hit is not None and now_mono < hit[0]:
                return _json_cached(hit[1], max_age=ttl if anon else 0)

        # Build chronological date list
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur)
            cur += timedelta(days=1)

        # Only each night's best events (highlight_events.TOP_EVENTS) are
        # kept, plus running counts. Holding every formatted detection across
        # a wide range (365 days x thousands/night) spiked worker RSS by
        # multiple GB; the page never needed the rest.
        candidates: list[dict] = []
        total_detections = 0  # running count (not the full list)
        dual_events    = 0
        shower_counts  = {}
        monthly_det    = {}   # YYYY-MM -> int
        monthly_ev     = {}   # YYYY-MM -> int

        def _visible_witness(w: dict) -> bool:
            if not anon:
                return True
            return (w.get("camera") or "").upper() in (_public_cams or set())

        def fetch_date(d):
            date_yyyymmdd = d.strftime("%Y%m%d")
            date_iso      = d.strftime("%Y-%m-%d")
            month_str     = d.strftime("%Y-%m")
            # Aggregate view: serve every night (incl. today) from the index /
            # archive, never the per-camera station HTTP fan-out — keeps a wide
            # range fast and off the storage box.
            payload = compute_detections_payload(date_yyyymmdd, allow_live=False)
            raw     = payload.get("detections", []) or []
            visible = [det for det in raw if _visible_to_request(det)]
            showers = [((det.get("rms") or {}).get("shower") or "").upper()
                       for det in visible]

            gmn_events = gmn_data.events_for_date(date_iso).get("events") or []
            ev_cnt = sum(1 for e in gmn_events if len(e.get("stations") or []) >= 2)
            # Marked, never dropped here: night_events applies the event rule.
            marked = hidden_detections.apply_payload(payload, hidden_snap, True)
            night = hl_events.night_events(
                [det for det in marked.get("detections") or [] if _visible_to_request(det)],
                gmn_events, marked.get("events") or [], date_iso,
                visible=_visible_witness, show_hidden=show_hidden,
            )
            return month_str, len(visible), showers, ev_cnt, night

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetch_date, d): d for d in dates}
            for fut in as_completed(futures):
                try:
                    month_str, n_det, showers, ev_cnt, night = fut.result()
                except Exception:
                    logger.exception("highlights: night %s failed", futures[fut])
                    continue
                total_detections += n_det
                candidates.extend(night)
                dual_events += ev_cnt
                monthly_det[month_str] = monthly_det.get(month_str, 0) + n_det
                monthly_ev[month_str]  = monthly_ev.get(month_str, 0) + ev_cnt
                for sh in showers:
                    if sh:
                        shower_counts[sh] = shower_counts.get(sh, 0) + 1

        # Orbit counts -- read daily GMN orbit tallies for each month in range
        months_in_range: set[tuple[int, int]] = set()
        cur = start.replace(day=1)
        while cur <= end:
            months_in_range.add((cur.year, cur.month))
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)

        new_orbits = 0
        for year, month in months_in_range:
            for date_str, count in gmn_data._daily_counts_for_month(year, month).items():
                try:
                    d = datetime.strptime(date_str, "%Y%m%d").date()
                    if start <= d <= end:
                        new_orbits += count
                except ValueError:
                    pass

        # Orbits this calendar month (1st of current month -> today UTC)
        today_utc   = datetime.now(timezone.utc).date()
        month_start = today_utc.replace(day=1)
        orbits_month_to_date = 0
        for date_str, count in gmn_data._daily_counts_for_month(today_utc.year, today_utc.month).items():
            try:
                d = datetime.strptime(date_str, "%Y%m%d").date()
                if month_start <= d <= today_utc:
                    orbits_month_to_date += count
            except ValueError:
                pass

        # Active cameras + locations from station cache
        active_stations = 0
        active_cameras  = 0
        for hk, st_cfg in config.stations.items():
            st = cache.get_status(hk) or {}
            if st.get("online"):
                active_stations += 1
                active_cameras  += len(st_cfg.cameras)

        # 90 km dual-station coverage (static network geometry)
        try:
            cov = coverage_mod.get_coverage_stats(platepar_store_mod.get_all())
            coverage_90km = round(cov.get("dual_coverage_pct", 0), 1)
        except Exception:
            coverage_90km = None

        # Main shower (prefer non-sporadic)
        main_shower = "SPO"
        non_spo = {k: v for k, v in shower_counts.items() if k not in ("SPO", "")}
        if non_spo:
            main_shower = max(non_spo, key=non_spo.get)
        elif shower_counts:
            main_shower = max(shower_counts, key=shower_counts.get)

        candidates.sort(key=hl_events.rank_key)
        top_events = candidates[:hl_events.TOP_EVENTS]
        hl_events.attach_reports(top_events, _visible_reports(anon))

        monthly_totals = [
            {
                "month":      m,
                "detections": monthly_det.get(m, 0),
                "events":     monthly_ev.get(m, 0),
            }
            for m in sorted(set(list(monthly_det) + list(monthly_ev)))
        ]

        payload = {
            "start":                 start_str,
            "end":                   end_str,
            "total_detections":      total_detections,
            "dual_station_events":   dual_events,
            "new_orbits":            new_orbits,
            "orbits_month_to_date":  orbits_month_to_date,
            "main_shower":           main_shower,
            "active_cameras":        active_cameras,
            "active_locations":      active_stations,
            "coverage_90km":         coverage_90km,
            "monthly_totals":        monthly_totals,
            "events":                top_events,
            # Lets the page merge a range it fetched in chunks.
            "shower_counts":         shower_counts,
        }

        with _highlights_lock:
            # Evict expired, then oldest, to cap memory.
            if len(_highlights_cache) >= _HIGHLIGHTS_MAX_ENTRIES:
                stale = [k for k, v in _highlights_cache.items() if v[0] <= now_mono]
                for k in stale:
                    _highlights_cache.pop(k, None)
                while len(_highlights_cache) >= _HIGHLIGHTS_MAX_ENTRIES:
                    _highlights_cache.pop(next(iter(_highlights_cache)), None)
            _highlights_cache[cache_key] = (time.monotonic() + ttl, payload)

        return _json_cached(payload, max_age=ttl if anon else 0)

    # ── Media serving endpoints ──────────────────────────────────────────

    @app.route("/api/highlights/shortclip/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    @limiter.limit("5 per minute")
    def highlights_shortclip(camera: str, date: str, filename: str):
        """Trim and serve a clip from the archive for the highlights page.

        Query params: ss=<seek_seconds> t=<duration_seconds>
        Returns an MP4 attachment cut by ``archive.trim_clip_response``: on the
        shared 2-lane media executor with its 24 s cap, never on the request
        thread, and the seek capped at 300 s like /api/archive/shortclip.
        """
        import public_api as _pub
        from routes.archive import trim_clip_response

        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(mkv|mp4)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)

        src = _pub._resolve_media_path(camera, date, filename, "meteors")
        if src is None:
            abort(404)

        try:
            ss = max(0.0, float(request.args.get("ss", 0)))
            t  = min(120.0, max(0.5, float(request.args.get("t", 20))))
        except ValueError:
            abort(400)

        resp = trim_clip_response(src, min(ss, 300.0), t, filename)
        if resp is None:
            abort(500)
        return resp

    @app.route("/api/highlights/clip/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    def highlights_clip(camera: str, date: str, filename: str):
        """Serve a full clip from the archive for the highlights modal.

        Public under the "highlights" page toggle. Only clips referenced by
        the curated /api/highlights/data top-10 are ever linked, and the
        camera/date/filename are strictly validated below before any archive
        path is resolved, so this cannot enumerate arbitrary station media.
        """
        import public_api as _pub
        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(mkv|mp4)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)
        # ?download=1 hands the clip out as an attachment (the modal's
        # "Download clip"): the same MP4 the player gets, unless ?format=mkv.
        as_attachment = request.args.get("download") == "1"
        if filename.endswith(".mkv") and request.args.get("format") != "mkv":
            # Safari and iOS cannot play MKV, and this modal is the only
            # consumer, so play the stream-copied MP4: hot cache, archived
            # .mp4 sibling, else a bounded remux (?format=mkv for the original).
            import clip_cache

            def _as_download(resp: Response) -> Response:
                if as_attachment:
                    resp.headers["Content-Disposition"] = (
                        f'attachment; filename="{filename[:-4]}.mp4"')
                return resp

            hot = clip_cache.serve_hot_mp4(camera, date.replace("-", ""), filename)
            if hot is not None:
                return _as_download(hot)
            sibling = _pub._resolve_media_path(camera, date, filename[:-4] + ".mp4", "meteors")
            if sibling is not None:
                resp = send_file(sibling, mimetype="video/mp4", conditional=True)
                resp.headers["Accept-Ranges"] = "bytes"
                resp.headers["Cache-Control"] = "private, max-age=3600"
                return _as_download(resp)
            src = _pub._resolve_media_path(camera, date, filename, "meteors")
            if src is None:
                abort(404)
            try:
                return _as_download(_pub._serve_as_mp4(src, filename))
            except _pub._RemuxBusy:
                busy = jsonify({"error": "remux_busy", "retry_after_seconds": 5})
                busy.status_code = 503
                busy.headers["Retry-After"] = "5"
                return busy
        path = _pub._resolve_media_path(camera, date, filename, "meteors")
        if path is None:
            abort(404)
        mime = "video/x-matroska" if filename.endswith(".mkv") else "video/mp4"
        resp = send_file(path, mimetype=mime, conditional=True,
                         as_attachment=as_attachment,
                         download_name=filename if as_attachment else None)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.route("/api/highlights/stack/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    def highlights_stack(camera: str, date: str, filename: str):
        """Serve a stack image from the archive for the highlights modal.

        Public under the "highlights" page toggle; same validated-path
        rationale as ``highlights_clip``.
        """
        import public_api as _pub
        import mimetypes as _mt
        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(webp|jpg|png)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)
        for subdir in ("stacks", "meteors"):
            path = _pub._resolve_media_path(camera, date, filename, subdir)
            if path is not None:
                mime = _mt.guess_type(filename)[0] or "image/webp"
                resp = send_file(path, mimetype=mime, conditional=True)
                resp.headers["Cache-Control"] = "private, max-age=3600"
                return resp
        abort(404)
