import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agents.local_roles import LocalHead, LocalReviewer, check_base_url
from schemas.order import OrderSchema


class _H(BaseHTTPRequestHandler):
    reply = '{"verdict":"pass","p0":[],"p1":[],"p2":[],"summary":"ok"}'

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        out = json.dumps({"choices": [{"message": {"content": _H.reply}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_rejects_non_loopback():
    with pytest.raises(ValueError):
        check_base_url("http://example.com/v1")


def test_reviewer_pass_and_provenance(server):
    _H.reply = '{"verdict":"pass","p0":[],"p1":[],"p2":[],"summary":"ok"}'
    r = LocalReviewer(base_url=server, model="m")
    order = OrderSchema(goal="g", allowed_paths=["a.py"], acceptance_criteria=["x"])
    res = r.review(order, "diff", {})
    assert res.verdict == "pass"
    p = r.get_provenance()
    assert p.provider == "local" and p.api_key_used is False and p.agent == "REVIEWER"


def test_reviewer_fail_closed_on_garbage(server):
    _H.reply = "not json"
    order = OrderSchema(goal="g", allowed_paths=["a.py"], acceptance_criteria=["x"])
    assert LocalReviewer(base_url=server).review(order, "d", {}).verdict == "changes_required"


def test_reviewer_fail_closed_when_server_down():
    order = OrderSchema(goal="g", allowed_paths=["a.py"], acceptance_criteria=["x"])
    res = LocalReviewer(base_url="http://127.0.0.1:1/v1", timeout=2).review(order, "d", {})
    assert res.verdict == "changes_required"


def test_head_invoke(server):
    _H.reply = "hello"
    assert LocalHead(base_url=server)._invoke_cli("hi") == "hello"


def test_role_settings_current_accepts_local():
    from core.role_settings import current
    cfg = {"head": {"provider": "claude"}, "builder": {"provider": "codex"}, "reviewer": {"provider": "local"}}
    assert current(cfg)["reviewer"] == "local"


def test_resolve_roles_keeps_local_reviewer(tmp_path):
    from core.provider_manager import ProviderManager
    pm = ProviderManager(db_path=str(tmp_path / "pm.db"))
    for strict in (False, True):
        out = pm.resolve_roles("claude", "codex", "local", strict=strict)
        assert out["reviewer_provider"] == "local"


def test_local_status(server):
    from core.local_status import local_status
    cfg = {"local_llm": {"base_url": server, "model": "m"}, "reviewer": {"provider": "local"}, "head": {"provider": "claude"}}
    assert local_status(cfg)["roles"] == ["REVIEWER"]
    down = local_status({"local_llm": {"base_url": "http://127.0.0.1:1/v1"}}, timeout=1)
    assert down["online"] is False and down["error"]
