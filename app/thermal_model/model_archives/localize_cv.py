"""
Archive — pure CV localisation (no model, no PyTorch).

This is a self-contained hotspot detector using only OpenCV HSV thresholding.
No CNN classification step — every frame is processed directly.

Advantages over CNN+HSV:
  - No trained model required
  - No PyTorch dependency
  - Runs on any machine with OpenCV
  - Fully explainable — every decision is traceable to a pixel value

Trade-off:
  Misses medium-temperature anomalies. The gap between equipment zones at 24×32 is
  only ~7 pixels. A medium-temperature anomaly (yellow-green, hue 35-60) in that gap
  cannot be reliably separated from equipment edge bleed at this resolution without
  raising the threshold high enough to cause false positives.

  The CNN catches these cases because it was trained on the specific pattern.

Recommendation for EQL:
  Deploy CV-only first (immediate, no training data needed), collect normal + anomaly
  images over 1-2 weeks, then fine-tune the CNN and switch to CNN+HSV for full coverage.
  See thermal_detection_approach.md for setup time estimates.

Test results (24×32 camera, two hot-water-cup test setup):
  normal_1–4     → no detection  ✓
  noisy_normal   → no detection  ✓
  hotspot_1      → detected      ✓
  hotspot_3      → detected      ✓
  hotspot_2      → missed        ✗  (medium-temp anomaly between zones — CNN catches it)
  coldspot_1     → no detection  ✓
  faint_hotspot  → no detection  ✗  (below threshold for both approaches)
"""

import cv2
import numpy as np

CAM_H, CAM_W = 24, 32


def warm_mask(img_bgr, hue_high=35, min_saturation=80, min_value=80):
    """
    Build a binary mask of warm/hot pixels using three HSV ranges.

    Red wraps in HSV (hue 0-8 AND 165-180 are both red), so both ends of the
    hue wheel are always included regardless of hue_high.
    """
    hsv    = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s, v   = min_saturation, min_value
    lo_red = cv2.inRange(hsv, np.array([0,   s, v]), np.array([8,        255, 255]))
    orange = cv2.inRange(hsv, np.array([8,   s, v]), np.array([hue_high, 255, 255]))
    hi_red = cv2.inRange(hsv, np.array([165, s, v]), np.array([180,      255, 255]))
    return cv2.bitwise_or(cv2.bitwise_or(lo_red, orange), hi_red)


def localize_and_draw_cv(image_path, zones, output_path=None,
                         hue_high=35, min_saturation=80, min_value=80, min_area=4):
    """
    Detect and annotate hotspots using pure HSV thresholding — no model needed.

    Args:
        image_path:     path to the thermal PNG
        zones:          list of zone dicts from SUBSTATION_ZONES (24×32 coordinates)
        output_path:    where to save; overwrites in place if None
        hue_high:       upper hue bound for warm range (default 35 = orange/yellow)
        min_saturation: minimum HSV saturation (default 80)
        min_value:      minimum HSV brightness (default 80)
        min_area:       minimum blob area in pixels to count as a hotspot (default 4)

    Returns:
        (save_path, detections) where detections is a list of (cx, cy, radius) tuples
    """
    img_cv       = cv2.imread(image_path)
    img_h, img_w = img_cv.shape[:2]
    scale_x      = img_w / CAM_W
    scale_y      = img_h / CAM_H

    # green boxes for safe zones
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        cv2.rectangle(img_cv, (x1, y1), (x2, y2), (0, 200, 0), 1)

    mask = warm_mask(img_cv, hue_high, min_saturation, min_value)

    # zero out safe zones
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        mask[y1:y2, x1:x2] = 0

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    detections  = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(contour)
        cx, cy, r   = int(cx), int(cy), max(int(r) + 1, 2)
        cv2.circle(img_cv, (cx, cy), r, (0, 0, 255), 1)
        cv2.circle(img_cv, (cx, cy), 1, (0, 0, 255), -1)
        detections.append((cx, cy, r))

    save_path = output_path or image_path
    cv2.imwrite(save_path, img_cv)
    return save_path, detections
