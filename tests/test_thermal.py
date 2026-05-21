def test_thermal_view_page_returns_200(client):
    resp = client.get("/thermal")
    assert resp.status_code == 200