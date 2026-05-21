"""
Run localisation on all test images and save annotated results.

Classification : CNN (hotspot_cam_model.pth)
Localisation   : HSV warm-colour thresholding — reads temperature directly
                 from the false-colour image, giving pixel-accurate circles.

Output per image:
  - green boxes  : known equipment zones (allowed to be hot, masked out)
  - red circle   : anomalous hotspot detected outside those zones

Usage:
    python test_localisation.py
    python test_localisation.py --substation Substation_Beta_Cam1

Tuning:
    --hue-low / --hue-high   : primary warm hue range (default 0-35, orange→yellow)
    --min-saturation         : min saturation  (default 80)
    --min-value              : min brightness  (default 80)
    --min-area               : min blob size in pixels (default 1)

NOTE: red wraps in HSV (hue 0-8 AND 165-180 are both red).
      Both ranges are always checked so very-hot red blobs are never missed.
"""

import os
import sys
import argparse
import numpy as np
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image

from substation_configs import SUBSTATION_ZONES

# paths
DIR        = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(DIR, "hotspot_cam_model.pth")
TEST_DIR   = os.path.join(DIR, "..", "Alert System tests")
OUTPUT_DIR = os.path.join(DIR, "..", "Alert System tests", "localisation_output")

CAM_H, CAM_W = 24, 32


# model
class HotspotCAM(nn.Module):
    def __init__(self, in_channels=3, num_classes=2):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(in_channels, 32, 3, padding=1), nn.BatchNorm2d(32),  nn.ReLU(inplace=True), nn.MaxPool2d(2, 2),
            nn.Conv2d(32, 64, 3, padding=1),          nn.BatchNorm2d(64),  nn.ReLU(inplace=True), nn.MaxPool2d(2, 2),
            nn.Conv2d(64, 128, 3, padding=1),         nn.BatchNorm2d(128), nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 3, padding=1),        nn.BatchNorm2d(128), nn.ReLU(inplace=True),
        )
        self.gap        = nn.AdaptiveAvgPool2d(1)
        self.dropout    = nn.Dropout(0.5)
        self.classifier = nn.Linear(128, num_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.gap(x)
        return self.classifier(self.dropout(torch.flatten(x, 1)))


# load model
checkpoint  = torch.load(MODEL_PATH, map_location="cpu")
CLASS_NAMES = checkpoint["class_names"]
HOTSPOT_IDX = checkpoint["hotspot_idx"]
CONF_THRESH = checkpoint["conf_threshold"]

if torch.backends.mps.is_available():
    device = torch.device("mps")
elif torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

model = HotspotCAM(in_channels=3, num_classes=len(CLASS_NAMES)).to(device)
model.load_state_dict(checkpoint["state_dict"])
model.eval()

eval_transform = transforms.Compose([
    transforms.Resize((CAM_H, CAM_W)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
])


# classification
def classify(image_path):
    img_tensor = eval_transform(Image.open(image_path).convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        prob = F.softmax(model(img_tensor), dim=1)[0][HOTSPOT_IDX].item()
    return prob >= CONF_THRESH, round(prob * 100, 1)


# warm-pixel mask
def warm_mask(img_bgr, args):
    """
    Build a binary mask of warm/hot pixels using HSV thresholding.

    Red wraps in HSV so we combine three regions:
      1. Low red   : hue 0  -> 8          (very hot, bottom of hue wheel)
      2. Orange    : hue 8  -> hue_high   (primary warm range, user-tunable)
      3. High red  : hue 165 -> 180       (very hot, top of hue wheel wrap-around)

    This ensures red blobs from any colormap are never silently skipped.
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s, v = args.min_saturation, args.min_value

    lo_red  = cv2.inRange(hsv, np.array([0,   s, v]), np.array([8,         255, 255]))
    orange  = cv2.inRange(hsv, np.array([8,   s, v]), np.array([args.hue_high, 255, 255]))
    hi_red  = cv2.inRange(hsv, np.array([165, s, v]), np.array([180,       255, 255]))

    return cv2.bitwise_or(cv2.bitwise_or(lo_red, orange), hi_red)


# localisation
def find_hotspot_contours(img_bgr, zones, scale_x, scale_y, args):
    mask = warm_mask(img_bgr, args)

    # zero out known equipment zones
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        mask[y1:y2, x1:x2] = 0

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [c for c in contours if cv2.contourArea(c) >= args.min_area]


# annotate
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

    is_anomaly, confidence = classify(image_path)
    detections = []

    if is_anomaly:
        contours = find_hotspot_contours(img_cv, zones, scale_x, scale_y, args)
        for contour in contours:
            (cx, cy), r = cv2.minEnclosingCircle(contour)
            cx, cy, r   = int(cx), int(cy), max(int(r) + 1, 2)
            cv2.circle(img_cv, (cx, cy), r, (0, 0, 255), 1)
            cv2.circle(img_cv, (cx, cy), 1, (0, 0, 255), -1)
            detections.append((cx, cy, r))

    return img_cv, {"is_anomaly": is_anomaly, "confidence": confidence, "detections": detections}


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
    parser.add_argument("--min-area",       type=float, default=1.0)
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
        annotated, result = annotate(path, substation_id=args.substation, args=args)

        rel      = os.path.relpath(path, TEST_DIR)
        out_path = os.path.join(OUTPUT_DIR, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        cv2.imwrite(out_path, annotated)

        status = "HOTSPOT" if result["is_anomaly"] else "normal "
        blobs  = f"  {len(result['detections'])} blob(s) circled" if result["detections"] else \
                 "  [CNN flagged but no warm pixels found outside zones]" if result["is_anomaly"] else ""
        print(f"[{status}] {result['confidence']:5.1f}%  {rel}{blobs}")

    print(f"\nDone. Saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
