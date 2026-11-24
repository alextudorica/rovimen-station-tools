"""Per-product colour overrides: color_post_video / _stack / _night.

A chunk stack is a per-pixel max over its chunk, so one color_post cannot suit
both it and the clip; the 2026-09-18 review tuned stacks and left clips visibly
off. Each product now merges its own override over the camera's color_post.
"""

from __future__ import annotations

import numpy as np
import pytest

import color_calibration as cc

# These cover the LEGACY color_post pipeline, which a camera only runs with
# color_adaptive disabled (sky match is the fleet default).
BASE = {"color_post": {"gamma": 0.9, "contrast": 1.1, "target_r": 1.02},
        "color_adaptive": {"enabled": False}}


def test_no_product_is_the_base_merge() -> None:
    post = cc.resolve_color_post(BASE)
    assert post["gamma"] == 0.9 and post["contrast"] == 1.1 and post["target_r"] == 1.02
    assert post["knee_low"] == cc.FLEET_DEFAULT_COLOR_POST["knee_low"]


@pytest.mark.parametrize("product", ["video", "stack", "night"])
def test_product_without_override_equals_base(product: str) -> None:
    assert cc.resolve_color_post(BASE, product) == cc.resolve_color_post(BASE)


def test_product_override_changes_only_its_keys() -> None:
    cfg = {**BASE, "color_post_video": {"gamma": 0.7}, "color_post_stack": {"contrast": 1.3}}
    video = cc.resolve_color_post(cfg, "video")
    stack = cc.resolve_color_post(cfg, "stack")
    assert (video["gamma"], video["contrast"]) == (0.7, 1.1)
    assert (stack["gamma"], stack["contrast"]) == (0.9, 1.3)
    assert video["target_r"] == stack["target_r"] == 1.02


def test_unknown_product_is_rejected() -> None:
    with pytest.raises(ValueError):
        cc.resolve_color_post(BASE, "clip")


def test_frame_calibration_uses_the_product_gamma() -> None:
    ref = np.full((8, 8, 3), 100, dtype=np.uint8)
    cfg = {**BASE, "color_post_stack": {"gamma": 1.3}}
    assert cc.resolve_calibration_from_frame(ref, cfg)[3] == 0.9
    assert cc.resolve_calibration_from_frame(ref, cfg, "stack")[3] == 1.3


def _sky(level: int = 150) -> np.ndarray:
    rng = np.random.default_rng(0)
    img = np.full((64, 96, 3), level, dtype=np.int16) + rng.integers(-6, 7, (64, 96, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def test_night_tone_pass_is_identity_without_override() -> None:
    img = _sky()
    a = cc.rebalance_composite(img, BASE)
    b = cc.rebalance_composite(img, {**BASE, "color_post_night": {}})
    assert np.array_equal(a, b)


def test_night_tone_pass_darkens_an_overbright_composite() -> None:
    img = _sky(180)
    plain = cc.rebalance_composite(img, BASE)
    darker = cc.rebalance_composite(img, {**BASE, "color_post_night": {"gamma": 1.6}})
    assert darker.mean() < plain.mean() - 20
