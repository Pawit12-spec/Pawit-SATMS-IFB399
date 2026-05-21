"""
Thermal model unit tests.

Covers:
  - analyse_image: result structure, confidence range, label consistency
  - analyse_image: classification accuracy on known hotspot and normal images
  - thermal_analytics: correct analysis text at each confidence tier
"""

import pytest
from pathlib import Path

torch = pytest.importorskip("torch", reason="PyTorch not installed — skipping thermal model tests")

from app.thermal_model.model import analyse_image, thermal_analytics

IMAGES_DIR = Path(__file__).parent / "alert_test_images"


# ── result structure ──────────────────────────────────────────────────────────

def test_analyse_image_returns_expected_keys():
    result = analyse_image(str(IMAGES_DIR / "normal" / "normal_2.png"))
    assert set(result.keys()) == {"is_anomaly", "confidence", "label"}


def test_analyse_image_confidence_is_percentage():
    result = analyse_image(str(IMAGES_DIR / "normal" / "normal_2.png"))
    assert 0.0 <= result["confidence"] <= 100.0


def test_analyse_image_label_matches_is_anomaly():
    for name in ["normal_2.png", "normal_3.png"]:
        result = analyse_image(str(IMAGES_DIR / "normal" / name))
        expected = "irregular_hotspot" if result["is_anomaly"] else "normal"
        assert result["label"] == expected


# ── classification accuracy ───────────────────────────────────────────────────

@pytest.mark.parametrize("filename", ["hotspot_1.png", "hotspot_2.png", "hotspot_3.png"])
def test_hotspot_images_flagged_as_anomaly(filename):
    result = analyse_image(str(IMAGES_DIR / "irregular_hotspot" / filename))
    assert result["is_anomaly"] is True, (
        f"{filename} should be flagged as anomaly (confidence={result['confidence']}%)"
    )


@pytest.mark.parametrize("filename", [
    "normal_2.png", "normal_3.png", "normal_4.png", "normal_5.png"
])
def test_normal_images_not_flagged(filename):
    result = analyse_image(str(IMAGES_DIR / "normal" / filename))
    assert result["is_anomaly"] is False, (
        f"{filename} should not be flagged (confidence={result['confidence']}%)"
    )


# ── thermal_analytics ─────────────────────────────────────────────────────────

def test_thermal_analytics_normal_returns_empty_analysis():
    result = thermal_analytics({"is_anomaly": False, "confidence": 95.0})
    assert result["analysis"] == []


def test_thermal_analytics_high_confidence_mentions_inspection():
    result = thermal_analytics({"is_anomaly": True, "confidence": 90.0})
    assert len(result["analysis"]) == 1
    assert "immediate inspection" in result["analysis"][0].lower()


def test_thermal_analytics_moderate_confidence():
    result = thermal_analytics({"is_anomaly": True, "confidence": 75.0})
    assert len(result["analysis"]) == 1
    assert "moderate" in result["analysis"][0].lower()


def test_thermal_analytics_low_confidence():
    result = thermal_analytics({"is_anomaly": True, "confidence": 50.0})
    assert len(result["analysis"]) == 1
    assert "low confidence" in result["analysis"][0].lower()
