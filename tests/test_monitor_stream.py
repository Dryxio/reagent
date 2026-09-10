"""Exercise real WebSocket initialization, updates, and native event attribution."""
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
from wsproto import ConnectionType, WSConnection
from wsproto.events import CloseConnection, Request, TextMessage

from re_agent.monitor.events import AgentEvents
from re_agent.monitor.server import Monitor, make_server
from re_agent.utils.storage import atomic_json


def test_websocket_initial_snapshot_then_live_update(tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"functions": {}}')
    server = make_server(Monitor(tmp_path, tmp_path / "state", ["session.json"]), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ws = WSConnection(ConnectionType.CLIENT)
    host = f"127.0.0.1:{server.server_port}"
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            sock.sendall(ws.send(Request(host=host, target="/api/stream",
                                         extra_headers=[(b"Origin", f"http://{host}".encode())])))
            pending = ""

            def receive():
                nonlocal pending
                while True:
                    ws.receive_data(sock.recv(65536))
                    for event in ws.events():
                        if isinstance(event, TextMessage):
                            pending += event.data
                            if event.message_finished:
                                result = json.loads(pending)
                                pending = ""
                                return result

            initial = receive()
            assert initial["type"] == "snapshot"
            assert initial["data"]["completed"] == 0
            atomic_json(path, {"functions": {"a": {"address": "100", "success": True}}})
            for _ in range(10):
                update = receive()
                if update["data"]["completed"] == 1:
                    break
            assert update["type"] == "update"
            assert update["data"]["completed"] == 1
            sock.sendall(ws.send(CloseConnection(code=1000)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_native_events_handle_partial_lines_and_keep_agents_separate(tmp_path):
    path = tmp_path / "native.jsonl"
    reader = AgentEvents()
    path.write_bytes(b'{"type":"text","data":"shared')
    assert reader.read(path) == []
    with path.open('ab') as stream:
        stream.write(b' text"}\n')
    assert reader.read(path)[0]["text"] == "shared text"
    for agent in ("child-a", "child-b"):
        reader.consume({"type": "tool_call_update", "rawOutput": {"MultiResult": {"results": [
            {"task_id": agent, "command": agent, "status": "completed", "output": f"code for {agent}"}
        ]}}}, "batch")
    assert reader.agents["child-a"]["text"] == "code for child-a"
    assert reader.agents["child-b"]["text"] == "code for child-b"
    reader.consume({"type": "thought", "data": "not an activity event"}, "batch")
    assert "not an activity event" not in str(reader.agents)
    for n in range(200):
        reader.agent(str(n))
    assert len(reader.agents) == 128


def test_stream_rejects_foreign_origin_and_history_cannot_escape(tmp_path):
    server = make_server(Monitor(tmp_path, tmp_path / "state", [], event_glob="*.jsonl"), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(base + "/api/stream", headers={
            "Origin": "https://untrusted.example", "Upgrade": "websocket"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/agent-history?source=../secret.jsonl")
        assert error.value.code == 404
        (tmp_path / "test.jsonl").write_text('{"type":"text","data":"<script>inert</script>"}\n')
        with urllib.request.urlopen(base + "/api/agent-history?source=test.jsonl") as response:
            assert json.load(response)["agents"][0]["text"] == "<script>inert</script>"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
