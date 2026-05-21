def test_humidity_page_returns_200(client):
    resp = client.get("/humidity")
    assert resp.status_code == 200