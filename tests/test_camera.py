import io
from PIL import Image

def make_test_png_bytes():
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10))
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def test_submit_camera_ok(client, tmp_path, monkeypatch):
    # If your upload dir is hardcoded to "uploads", tests will write there.
    # Better: make upload dir configurable and point it to tmp_path.
    # For now, this just shows the upload call pattern.

    data = {
        "image": (make_test_png_bytes(), "test.png"),
    }
    resp = client.post("/submit/camera", data=data, content_type="multipart/form-data")
    assert resp.status_code == 201
    j = resp.get_json()
    assert "id" in j
    assert j["filename"].endswith(".png")


def test_submit_camera_missing_field(client):
    resp = client.post("/submit/camera", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
