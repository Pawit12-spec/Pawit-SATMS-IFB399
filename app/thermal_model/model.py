import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import os
import cv2
import numpy as np


class HotspotCAM(nn.Module):
    def __init__(self, in_channels=3, num_classes=2):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(in_channels, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),

            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),

            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),

            nn.Conv2d(128, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
        )
        self.gap        = nn.AdaptiveAvgPool2d(1)
        self.dropout    = nn.Dropout(0.5)
        self.classifier = nn.Linear(128, num_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.gap(x)
        x = torch.flatten(x, 1)
        return self.classifier(self.dropout(x))

    def generate_cam(self, image_tensor, class_idx):
        self.eval()
        with torch.no_grad():
            feat_maps = self.features(image_tensor)  # [1, 128, 6, 8]
        weights = self.classifier.weight[class_idx].detach()  # [128]
        cam = torch.einsum('c,cyx->yx', weights, feat_maps[0])
        cam = F.relu(cam).cpu().numpy()
        if cam.max() > 0:
            cam = cam / cam.max()
        return cam


if torch.backends.mps.is_available():
    device = torch.device("mps")
elif torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

current_dir = os.path.dirname(os.path.abspath(__file__))
checkpoint  = torch.load(os.path.join(current_dir, 'hotspot_cam_model.pth'), map_location=device)

HOTSPOT_IDX    = checkpoint["hotspot_idx"]
CAM_THRESHOLD  = checkpoint["cam_threshold"]
CONF_THRESHOLD = checkpoint["conf_threshold"]
IMG_H          = checkpoint["img_h"]
IMG_W          = checkpoint["img_w"]

classifier = HotspotCAM(in_channels=3, num_classes=len(checkpoint["class_names"]))
classifier.load_state_dict(checkpoint["state_dict"])
classifier.to(device)
classifier.eval()

transform = transforms.Compose([
    transforms.Resize((IMG_H, IMG_W)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
])


def analyse_image(path):
    img_tensor = transform(Image.open(path).convert("RGB")).unsqueeze(0).to(device)

    with torch.no_grad():
        probs        = F.softmax(classifier(img_tensor), dim=1)
        hotspot_prob = probs[0][HOTSPOT_IDX].item()

    is_anomaly = hotspot_prob >= CONF_THRESHOLD
    return {
        "is_anomaly": is_anomaly,
        "confidence": round(hotspot_prob * 100, 2) if is_anomaly else round((1 - hotspot_prob) * 100, 2),
        "label":      "irregular_hotspot" if is_anomaly else "normal",
    }


def thermal_analytics(result):
    analysis = []
    if not result.get("is_anomaly"):
        return {"analysis": analysis}

    confidence = result.get("confidence", 0)

    if confidence >= 85:
        analysis.append("High confidence irregular hotspot — possible equipment overheating or electrical fault, immediate inspection required")
    elif confidence >= 70:
        analysis.append("Moderate confidence hotspot — possible localised heat buildup, verify equipment condition")
    else:
        analysis.append("Low confidence hotspot — possible minor thermal irregularity, monitor closely")

    return {"analysis": analysis}


def localize_and_draw(image_path, camera_id="Substation_Alpha_Cam1", output_path=None):
    """
    Localise the hotspot using HSV colour thresholding and annotate the image.

    Classification is already done by analyse_image() — this function only handles
    WHERE the hotspot is. It reads temperature directly from the false-colour image:
    orange/yellow pixels are warm, blue pixels are cold.

    Red wraps in HSV (hue 0-8 AND 165-180), so three ranges are checked to avoid
    missing very-hot blobs that appear red on the colormap.

    Equipment zones from substation_configs are masked out before detection so
    normal equipment heat does not trigger false circles.

    Args:
        image_path:  path to the thermal PNG to annotate
        camera_id:   camera identifier, used to look up zone coordinates
        output_path: where to save the annotated image; overwrites in place if None

    Returns:
        (save_path, detections) where detections is a list of (cx, cy, radius) tuples.
        Empty list means CNN flagged a hotspot but no warm pixels were found outside
        the equipment zones (possible very faint or borderline case).
    """
    from .substation_configs import SUBSTATION_ZONES

    CAM_H, CAM_W = 24, 32

    img_cv       = cv2.imread(image_path)
    img_h, img_w = img_cv.shape[:2]
    scale_x      = img_w / CAM_W
    scale_y      = img_h / CAM_H
    zones        = SUBSTATION_ZONES.get(camera_id, [])

    # green boxes for known safe equipment zones
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        cv2.rectangle(img_cv, (x1, y1), (x2, y2), (0, 200, 0), 1)

    # build warm-pixel mask across three HSV ranges
    hsv    = cv2.cvtColor(img_cv, cv2.COLOR_BGR2HSV)
    lo_red = cv2.inRange(hsv, np.array([0,   80, 80]), np.array([8,   255, 255]))
    orange = cv2.inRange(hsv, np.array([8,   80, 80]), np.array([35,  255, 255]))
    hi_red = cv2.inRange(hsv, np.array([165, 80, 80]), np.array([180, 255, 255]))
    mask   = cv2.bitwise_or(cv2.bitwise_or(lo_red, orange), hi_red)

    # zero out safe zones so equipment heat is ignored
    for zone in zones:
        x1 = int(zone["startX"] * scale_x)
        y1 = int(zone["startY"] * scale_y)
        x2 = int(zone["endX"]   * scale_x)
        y2 = int(zone["endY"]   * scale_y)
        mask[y1:y2, x1:x2] = 0

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    detections  = []
    for contour in contours:
        if cv2.contourArea(contour) < 1:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(contour)
        cx, cy, r   = int(cx), int(cy), max(int(r) + 1, 2)
        cv2.circle(img_cv, (cx, cy), r, (0, 0, 255), 1)
        cv2.circle(img_cv, (cx, cy), 1, (0, 0, 255), -1)
        detections.append((cx, cy, r))

    save_path = output_path or image_path
    cv2.imwrite(save_path, img_cv)
    return save_path, detections
