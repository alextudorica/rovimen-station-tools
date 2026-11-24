"""Clip-name helpers behind the archive listings (routes/archive.py) and the
detection payloads: one listing entry per clip, and the right stack for the
faststart ``.mp4`` copy (it used to derive ``X_color_stack.webp``)."""

from __future__ import annotations

from route_helpers import clip_base, listed_clip_names, stack_name_for_clip


def test_stack_name_for_every_video_name_of_a_clip():
    for name in ("RO000W_20260920_213000_color.mkv",
                 "RO000W_20260920_213000_color.mp4",
                 "RO000W_20260920_213000_color.details.mkv",
                 "RO000W_20260920_213000_color.details.mp4"):
        assert stack_name_for_clip(name) == "RO000W_20260920_213000_stack.webp"
    # Boundary-stitched clips keep both times.
    assert (stack_name_for_clip("RO000W_20260920_213000_213020_color.mp4")
            == "RO000W_20260920_213000_213020_stack.webp")
    assert stack_name_for_clip("notes.txt") is None


def test_clip_base_maps_copies_to_the_mkv():
    assert clip_base("X_color.mp4") == "X_color.mkv"
    assert clip_base("X_color.details.mp4") == "X_color.mkv"
    assert clip_base("X_color.mkv") == "X_color.mkv"
    assert clip_base("X_night.mp4") == "X_night.mp4"


def test_listing_shows_one_entry_per_clip():
    files = (
        "A_color.mkv", "A_color.mp4", "A_color.details.mkv", "A_color.details.mp4",
        "B_color.mp4",                 # .mkv gone: the copy is the clip
        "C_color.mkv",
        "A_stack.webp",
    )
    assert listed_clip_names(files) == ["A_color.mkv", "B_color.mp4", "C_color.mkv"]
