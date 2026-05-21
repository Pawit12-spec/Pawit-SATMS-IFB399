def test_events_endpoint_has_sse_headers(client):
    resp = client.get("/events", buffered = False)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert "no-cache" in resp.headers.get("Cache-Control", "")
