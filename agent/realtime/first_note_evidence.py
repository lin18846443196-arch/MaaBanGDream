"""Shared note-rim evidence for cooperative startup and the native photogate."""

from typing import Any

import cv2
import numpy as np

from .playfield_monitor import LANE_CENTERS


def has_approaching_note_head(
    image: Any,
    *,
    from_row: int = 510,
    to_row: int = 535,
    reference_height: int = 720,
) -> bool:
    """Require an actual note rim near the timing band, not stage lights.

    TYPE1 white rims overlap the band before their head centre reaches it.
    Narrow vertical lights and the fixed judgement line are excluded by
    width/aspect ratio, band position, lane projection and nearby colour.
    """
    height, width = image.shape[:2]
    scale = height / float(reference_height)
    top = max(0, round((from_row - 65) * scale))
    bottom = min(height, round((to_row + 25) * scale))
    hsv = cv2.cvtColor(image[top:bottom, :, :3], cv2.COLOR_BGR2HSV)
    rim = cv2.inRange(hsv, (0, 0, 210), (179, 90, 255))
    rim = cv2.morphologyEx(rim, cv2.MORPH_OPEN,
                         np.ones((1, max(3, round(13 * scale))), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(rim)
    for x, y, w, h, area in stats[1:]:
        if not (45 * scale <= w <= 220 * scale and 1 <= h <= 36 * scale
                and w / max(h, 1) >= 3 and area >= 70 * scale * scale):
            continue
        if top + y + h < (from_row - 10) * scale:
            continue
        centre_y = (top + y + h / 2) / scale
        centre_x = (x + w / 2) * 1280.0 / width
        progress = (centre_y - 20.0) / (590.0 - 20.0)
        centres = [640 + (lane - 640) * progress for lane in LANE_CENTERS]
        if min(abs(centre_x - lane) for lane in centres) > 55:
            continue
        margin = max(1, round(20 * scale))
        colour = hsv[max(0, y - margin):min(hsv.shape[0], y + h + margin),
                     max(0, x - margin):min(width, x + w + margin)]
        saturated = (colour[:, :, 1] >= 70) & (colour[:, :, 2] >= 150)
        if np.count_nonzero(saturated) >= 25 * scale * scale:
            return True
    return False
