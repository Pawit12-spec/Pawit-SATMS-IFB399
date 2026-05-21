def test_co2_page_returns_200(client):
    resp = client.get("/co2")
    assert resp.status_code == 200