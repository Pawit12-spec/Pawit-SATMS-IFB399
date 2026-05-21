import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import os
import cv2
import numpy as np
from pytorch_grad_cam import GradCAMPlusPlus
from pytorch_grad_cam.utils.model_targets import ClassifierOutputTarget
from pytorch_grad_cam.utils.image import show_cam_on_image


class HotspotClassifier(nn.Module):
    """ Simple CNN classifier for Hotspot vs Normal """
    def __init__(self, in_channels=3, num_classes=2):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, 16, 3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.conv3 = nn.Conv2d(32, 64, 3, padding=1)
        self.pool = nn.MaxPool2d(2, 2)
        self.dropout = nn.Dropout(0.5)
        self.fc1 = nn.Linear(64 * 3 * 4, 128)
        self.fc2 = nn.Linear(128, num_classes)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = self.pool(F.relu(self.conv3(x)))
        x = torch.flatten(x, 1)
        x = self.dropout(F.relu(self.fc1(x)))
        return self.fc2(x)

# Device configuration (Mac vs standard server)
if torch.backends.mps.is_available():
    device = torch.device("mps")
else:
    device = torch.device("cpu")

# Load pre-trained binary classifier
current_dir = os.path.dirname(os.path.abspath(__file__))
classifier_path = os.path.join(current_dir, 'hotspot_binary_model.pth')

classifier = HotspotClassifier(in_channels=3, num_classes=2)
classifier.load_state_dict(torch.load(classifier_path, map_location=device))
classifier.to(device)
classifier.eval()

# Image transformation (Resize and Convert to Tensor)
transform = transforms.Compose([
    transforms.Resize((24, 32)),
    transforms.ToTensor(),
])

# Threshold for triggering an alert (50% is safe in our scenario)
CONFIDENCE_THRESHOLD = 0.50


def analyse_image(path):
    img = Image.open(path).convert("RGB")
    img_tensor = transform(img).unsqueeze(0).to(device)
    
    with torch.no_grad():
        logits = classifier(img_tensor)
        probabilities = F.softmax(logits, dim=1)
        hotspot_prob = probabilities[0][0].item()
        
    is_anomaly = hotspot_prob >= CONFIDENCE_THRESHOLD
    
    return {
        "is_anomaly": is_anomaly,
        "confidence": round(hotspot_prob * 100, 2) if is_anomaly else round((1 - hotspot_prob) * 100, 2),
        "label": "irregular_hotspot" if is_anomaly else "normal"
    }
    
    
    
target_layers = [classifier.conv2]
cam = GradCAMPlusPlus(model=classifier, target_layers=target_layers)

def localize_and_draw(image_path, output_dir="Alert_System/flagged_images"):
    """
    Uses Occlusion Sensitivity to pinpoint the exact pixels the CNN relies on.
    It slides a black box over the image and measures where confidence drops the most.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. Get the base image and the base prediction
    img_pil = Image.open(image_path).convert("RGB")
    original_tensor = transform(img_pil).unsqueeze(0).to(device)
    
    with torch.no_grad():
        base_logits = classifier(original_tensor)
        base_prob = F.softmax(base_logits, dim=1)[0][0].item() # Probability of Hotspot
        
    _, _, h, w = original_tensor.shape # (24 height, 32 width)
    
    # We will store the confidence drop for every pixel here
    heatmap = np.zeros((h, w))
    
    box_size = 4 # Size of the blackout box (4x4 pixels)
    
    # 2. Slide the blackout box across every pixel in the image
    for y in range(h - box_size):
        for x in range(w - box_size):
            
            # Create a fresh copy of the image tensor and black out a small square
            occluded_tensor = original_tensor.clone()
            occluded_tensor[:, :, y:y+box_size, x:x+box_size] = 0.0 
            
            # Ask the model for its confidence on this occluded image
            with torch.no_grad():
                logits = classifier(occluded_tensor)
                prob = F.softmax(logits, dim=1)[0][0].item()
                
            # Calculate how much the confidence dropped
            drop = base_prob - prob
            
            # Assign that drop value to the center of the blackout box
            center_y = y + (box_size // 2)
            center_x = x + (box_size // 2)
            
            # We only care if confidence dropped, ignore if it somehow went up
            heatmap[center_y, center_x] = max(0, drop)
            
    # 3. Find the exact coordinate that caused the maximum drop
    (minVal, maxVal, minLoc, maxLoc) = cv2.minMaxLoc(heatmap)
    
    # maxLoc is our (X, Y) coordinate on the 24x32 tensor.
    tensor_x, tensor_y = maxLoc
    
    # 4. Load the original OpenCV image to draw the circle
    original_cv_img = cv2.imread(image_path)
    img_h, img_w, _ = original_cv_img.shape
    
    # Scale the coordinates back up just in case the original OpenCV image is larger than 24x32
    final_x = int((tensor_x / w) * img_w)
    final_y = int((tensor_y / h) * img_h)
    
    # 5. Draw the circle!
    cv2.circle(original_cv_img, (final_x, final_y), radius=2, color=(0, 0, 255), thickness=1)
    
    # Save the annotated image
    filename = os.path.basename(image_path)
    save_path = os.path.join(output_dir, f"LOCATED_{filename}")
    cv2.imwrite(save_path, original_cv_img)
    
    return save_path, final_x, final_y

def debug_heatmap(image_path):
    print(f"Analyzing: {image_path}")
    
    # Load image
    img_pil = Image.open(image_path).convert("RGB")
    img_tensor = transform(img_pil).unsqueeze(0).to(device)
    
    # 1. FORCE PyTorch to track gradients (sometimes it turns off during eval)
    img_tensor.requires_grad_(True)
    
    # 2. Generate the raw heatmap
    targets = [ClassifierOutputTarget(0)]
    grayscale_cam = cam(input_tensor=img_tensor, targets=targets)[0, :]
    
    # 3. Prepare the original image for overlay
    img_cv = cv2.imread(image_path)
    img_cv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
    img_float = np.float32(img_cv) / 255.0  # Must be between 0 and 1
    
    # 4. Generate the overlaid glowing image
    cam_image = show_cam_on_image(img_float, grayscale_cam, use_rgb=True)
    
    # Save it to your root folder so you can find it easily
    cv2.imwrite("DEBUG_HEATMAP.png", cv2.cvtColor(cam_image, cv2.COLOR_RGB2BGR))
    print("Saved DEBUG_HEATMAP.png to your current folder!")

# Run it! Put the absolute path to a known hotspot image here:
debug_heatmap("EQL SATMS/app/Alert_System/Alert System tests/irregular_hotspot/hotspot_3.png")


"""
# Example usage:

result = analyse_image("Alert_System/Alert System tests/irregular_hotspot/hotspot_2.png")




print(f"Triggering Alert! Confidence: {result['confidence']}%")
    
"""
