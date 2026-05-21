"""
Pure image processing hotspot localisation — no model involved.

Logic:
  1. Mask out known equipment zones (allowed to be hot).
  2. HSV threshold to find warm/hot pixels outside those zones.
  3. Filter blobs by minimum area to ignore sensor noise.
  4. Draw enclosing circle around each remaining blob.

Output per image:
  - green boxes  : known equipment zones (excluded from detection)
  - red circle   : anomalous hotspot found outside those zones

Usage:
    python test_localisation_cv.py
    python test_localisation_cv.py --substation Substation_Beta_Cam1

Tuning:
    --hue-high        : upper hue bound for warm colours (default 35, orange/yellow)
    --min-saturation  : minimum saturation  (default 80)
    --min-value       : minimum brightness  (default 80)
    --min-area        : minimum blob area in pixels to count as a hotspot (default 4)

Red wraps in HSV — hue 0-8 and 165-180 are always checked regardless of --hue-high.
"""

import os
import sys
import argparse
import numpy as np
import cv2

from substation_configs import SUBSTATION_ZONES

# paths
DIR        = os.path.dirname(os.path.abspath(__file__))
TEST_DIR   = os.path.join(DIR, "..", "Alert System tests")
OUTPUT_DIR = os.path.join(DIR, "..", "Alert System tests", "localisation_output_cv")

# zone coordinates are defined in 24x32 pixel space
CAM_H, CAM_W = 24, 32


# warm-pixel mask
def warm_mask(img_bgr, args):
    """
    Binary mask of warm/hot pixels.
    Covers three HSV regions to handle the red wrap-around:
      0-8   : low red (very hot)
      8-hue_high : orange through yellow
      165-180: high red wrap-around (very hot)
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s, v = args.min_saturation, args.min_value

    lo_red = cv2.inRange(hsv, np.array([0,   s, v]), np.array([8,            255, 255]))
    orange = cv2.inRange(hsv, np.array([8,   s, v]), np.array([args.hue_high, 255, 255]))
    hi_red = cv2.inRange(hsv, np.array([165, s, v]), np.array([180,          255, 255]))

    return cv2.bitwise_or(cv2.bitwise_or(lo_red, orange), hi_red)


# annotate one image
def annotate(image_path, substation_id, args):
    img_cv       = cv2.imread(image_path)
    img_h, img_w = img_cv.shape[:2]
    scale_x      = img_w / CAM_W
    scale_y      = img_h / CAM_H
    zones        = SUBSTATION_ZONES.get(substation_id, [])

    # green boxes for safe zones
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        cv2.rectangle(img_cv, (x1, y1), (x2, y2), (0, 200, 0), 1)

    # build warm mask and zero out safe zones
    mask = warm_mask(img_cv, args)

    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        mask[y1:y2, x1:x2] = 0

    # find blobs and circle them
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    detections  = []

    for contour in contours:
        if cv2.contourArea(contour) < args.min_area:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(contour)
        cx, cy, r   = int(cx), int(cy), max(int(r) + 1, 2)
        cv2.circle(img_cv, (cx, cy), r, (0, 0, 255), 1)
        cv2.circle(img_cv, (cx, cy), 1, (0, 0, 255), -1)
        detections.append((cx, cy, r))

    return img_cv, detections


# collect images
def collect_images(test_dir):
    paths = []
    for root, _, files in os.walk(test_dir):
        if "localisation_output" in root:
            continue
        for f in files:
            if f.lower().endswith(".png"):
                paths.append(os.path.join(root, f))
    return sorted(paths)


# main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--substation",     default="Substation_Alpha_Cam1",
                        choices=list(SUBSTATION_ZONES.keys()))
    parser.add_argument("--hue-high",       type=int,   default=35)
    parser.add_argument("--min-saturation", type=int,   default=80)
    parser.add_argument("--min-value",      type=int,   default=80)
    parser.add_argument("--min-area",       type=float, default=4.0)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    images = collect_images(TEST_DIR)

    if not images:
        print(f"No PNG images found under {TEST_DIR}")
        sys.exit(1)

    print(f"Substation  : {args.substation}")
    print(f"Hue ranges  : 0-8 (red low) | 8-{args.hue_high} (orange/yellow) | 165-180 (red high)")
    print(f"Min sat/val : {args.min_saturation} / {args.min_value}")
    print(f"Min area    : {args.min_area}px")
    print(f"Images      : {len(images)}\n")

    for path in images:
        annotated, detections = annotate(path, substation_id=args.substation, args=args)

        rel      = os.path.relpath(path, TEST_DIR)
        out_path = os.path.join(OUTPUT_DIR, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        cv2.imwrite(out_path, annotated)

        status = "HOTSPOT" if detections else "normal "
        detail = f"  {len(detections)} blob(s) circled" if detections else ""
        print(f"[{status}]  {rel}{detail}")

    print(f"\nDone. Saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
