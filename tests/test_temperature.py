def test_temperature_page_returns_200(client):
    resp = client.get("/temperature")
    assert resp.status_code == 200