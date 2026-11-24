"""The station cron jobs that share a host with capture must yield to it.

RMS capture is the one realtime job on a station: it pulls an RTSP stream and
must keep grabbing 256-frame blocks on a 10.24 s cadence. Everything else the
project installs is maintenance with minutes of slack, so any of it that
competes for I/O shows up directly as lost FF files.

That is not hypothetical. The janitor cron shipped without any priority
controls while the dawn cron beside it had `nice -n 10 ionice -c 3`, and on a
constrained host the difference was measurable: at Cahul (Pi 5, 2 cameras)
capture stalls ran 22% per chunk in minutes 0-2 of each 10-minute period
against 4.6% in minutes 3-9, a 4.6-5.0x excess aligned with the only `*/10`
job on the box, reproduced on both cameras across three nights. Roughly 10% of
the night's FF files were lost this way, against 0.1-0.2% on a roomier host
running the identical janitor.

So these tests pin the priority prefix onto every cron this project installs
next to capture. They assert on the generated cron text rather than on a live
crontab because `toggle_rovimen.sh` is the single writer of that text and
`updater.sh --ensure-crons` re-applies it on every pull -- the file content IS
the deployed artifact.

The janitor's class is pinned separately and deliberately: it must be
best-effort, NOT the idle class the dawn cron uses. The janitor owns
disk-pressure escalation, so on a full disk -- the exact host where it matters
most -- an idle-class job could be starved indefinitely behind capture writes
and never reclaim the space. Best-effort at the lowest priority yields to
capture while still guaranteeing forward progress.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOGGLE = Path(__file__).resolve().parents[1] / "rovimen-scripts" / "toggle_rovimen.sh"
JANITOR = (Path(__file__).resolve().parents[1] / "rovimen-scripts"
           / "janitor_storage_watchdog.py")

# The crons that do sustained work next to capture. `reboot` is deliberately
# absent: reboot_guard.sh reads a flag file and either reboots or exits, so it
# has no I/O burst to deprioritise and pinning a prefix onto it would assert a
# rule that buys nothing.
CRON_JOBS = ("dawn", "janitor")


def _cron_line(job: str) -> str:
    """The schedule line from the `$SCRIPTS_DIR/.rovimen_cron_<job>` heredoc.

    Comment lines inside the heredoc are skipped: the assertions are about the
    command that actually runs, and the block carries several lines of
    rationale above it.
    """
    text = TOGGLE.read_text()
    m = re.search(
        r"cat > \"\$SCRIPTS_DIR/\.rovimen_cron_%s\" <<EOF\n(.*?)\nEOF" % re.escape(job),
        text, re.DOTALL,
    )
    assert m, "no heredoc found for the %s cron in %s" % (job, TOGGLE.name)
    lines = [ln for ln in m.group(1).splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(lines) == 1, "expected one schedule line for %s, got %r" % (job, lines)
    return lines[0]


def _command(line: str) -> str:
    """Everything after the five schedule fields and the user field."""
    return line.split(None, 6)[6]


class TestCapturePriority:
    """Nothing installed beside capture may run at default priority."""

    @pytest.mark.parametrize("job", CRON_JOBS)
    def test_cron_runs_niced(self, job: str) -> None:
        cmd = _command(_cron_line(job))
        assert re.match(r"(nice -n \d+ )?(ionice [^ ]+ [^ ]+ )*", cmd)
        assert "nice -n " in cmd, (
            "the %s cron runs at default CPU priority; it shares a host with "
            "realtime RTSP capture" % job
        )

    @pytest.mark.parametrize("job", CRON_JOBS)
    def test_cron_runs_with_lowered_io_priority(self, job: str) -> None:
        cmd = _command(_cron_line(job))
        assert "ionice -c " in cmd, (
            "the %s cron competes for disk with capture at default I/O "
            "priority" % job
        )

    def test_janitor_is_best_effort_not_idle(self) -> None:
        """Idle class would starve the one job that frees a full disk."""
        cmd = _command(_cron_line("janitor"))
        assert "ionice -c 2" in cmd, (
            "the janitor must run best-effort: it owns disk-pressure "
            "escalation and cannot be starved on a full disk"
        )
        assert "ionice -c 3" not in cmd

    def test_janitor_still_runs_every_ten_minutes(self) -> None:
        """Deprioritising is the fix; running less often is not.

        Pressure escalation is a safety net against ENOSPC, which has cost the
        fleet whole nights. The interval stays put.
        """
        assert _cron_line("janitor").startswith("*/10 * * * * ")

    def test_janitor_docstring_matches_the_installed_line(self) -> None:
        """The module documents its own cron entry; keep the two in step."""
        doc = JANITOR.read_text()
        assert "nice -n 19 ionice -c 2 -n 7" in doc, (
            "janitor_storage_watchdog.py still documents an unprioritised "
            "cron entry"
        )
