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


def localize_and_draw(image_path, output_dir="Alert_System/flagged_images"):
    os.makedirs(output_dir, exist_ok=True)

    img_tensor = transform(Image.open(image_path).convert("RGB")).unsqueeze(0).to(device)
    cam_raw    = classifier.generate_cam(img_tensor, HOTSPOT_IDX)

    img_cv       = cv2.imread(image_path)
    img_h, img_w = img_cv.shape[:2]

    cam_up    = np.clip(cv2.resize(cam_raw, (img_w, img_h), interpolation=cv2.INTER_CUBIC), 0, 1)
    cam_uint8 = (cam_up * 255).astype(np.uint8)
    _, binary = cv2.threshold(cam_uint8, int(CAM_THRESHOLD * 255), 255, cv2.THRESH_BINARY)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    final_x, final_y = img_w // 2, img_h // 2

    if contours:
        largest = max(contours, key=cv2.contourArea)
        (cx, cy), radius = cv2.minEnclosingCircle(largest)
        final_x, final_y = int(cx), int(cy)
        cv2.circle(img_cv, (final_x, final_y), max(int(radius) + 2, 3), (0, 0, 255), 1)
        cv2.circle(img_cv, (final_x, final_y), 1, (0, 0, 255), -1)

    save_path = os.path.join(output_dir, f"LOCATED_{os.path.basename(image_path)}")
    cv2.imwrite(save_path, img_cv)

    return save_path, final_x, final_y
