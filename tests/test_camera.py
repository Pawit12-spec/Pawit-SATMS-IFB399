import io
from pathlib import Path
from PIL import Image


def make_test_png_bytes():
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10))
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def test_submit_camera_ok(client):
    data = {"image": (make_test_png_bytes(), "test.png")}
    resp = client.post("/submit/camera", data=data, content_type="multipart/form-data")
    assert resp.status_code == 201
    j = resp.get_json()
    assert "id" in j
    assert j["filename"].endswith(".png")


def test_submit_camera_missing_field(client):
    resp = client.post("/submit/camera", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_localize_called_on_hotspot(client, monkeypatch):
    """When the CNN flags a hotspot, localize_and_draw should run on the saved image."""
    monkeypatch.setattr("app.routes.thermal_analyse_image",
                        lambda path: {"is_anomaly": True, "confidence": 95.0, "label": "irregular_hotspot"})
    monkeypatch.setattr("app.routes.thermal_analytics", lambda r: {"analysis": []})
    monkeypatch.setattr("app.routes.alert.HighPriorityAlert", lambda *a, **kw: type("A", (), {"trigger": lambda self: None})())

    localize_calls = []
    def fake_localize(path, camera_id="Substation_Alpha_Cam1", **kw):
        localize_calls.append(path)
        return (path, [(10, 10, 5)])
    monkeypatch.setattr("app.routes.thermal_localize", fake_localize)

    resp = client.post("/submit/camera",
                       data={"image": (make_test_png_bytes(), "test.png")},
                       content_type="multipart/form-data")

    assert resp.status_code == 201
    assert len(localize_calls) == 1
    assert localize_calls[0].endswith(".png")
