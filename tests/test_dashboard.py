def test_dashboard_page_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200