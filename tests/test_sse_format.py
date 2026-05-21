import json
from app.sse import _format_sse 

def test_sse_format_includes_event_and_data():
    payload = {"temperature_c": 30.1, "humidity_pct": 50}
    msg = _format_sse(payload, event="reading", id="123")

    assert "id: 123\n" in msg
    assert "event: reading\n" in msg

    assert "data: " in msg
    data_line = [line for line in msg.splitlines() if line.startswith("data: ")][0]
    parsed = json.loads(data_line[len("data: "):])
    assert parsed == payload

    assert msg.endswith("\n\n")
