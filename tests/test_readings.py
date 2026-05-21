def test_submit_readings_ok(client):
    payload = {
        "temperature_c": 22.5,
        "humidity_pct": 55.0,
        "co2_ppm": 67.0,
        "aqi": 67.0,
        "timestamp": "2026-01-18T12:00:00Z",

    }

    resp = client.post("/submit/readings", json=payload)
    assert resp.status_code == 201
    assert resp.get_json()["status"] == "ok"


def test_submit_readings_missing_json(client):
    resp = client.post("/submit/readings")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_submit_readings_bad_payload(client):
    payload = {"temperature_c": "nope"}
    resp = client.post("/submit/readings", json=payload)
    assert resp.status_code == 400
    assert "Bad payload" in resp.get_json()["error"]
