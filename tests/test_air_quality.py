def test_air_quality_page_returns_200(client):
    resp = client.get("/air-quality")
    assert resp.status_code == 200