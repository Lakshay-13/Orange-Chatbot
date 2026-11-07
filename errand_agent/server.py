"""Loopback-only HTTP app with same-origin, CSRF-protected local APIs."""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .agent import ErrandAgent
from .approvals import ApprovalError
from .clock import Clock
from .config import AppConfig
from .mock_provider import MockProvider, MockToolError
from .proactive import ProactiveMonitor
from .storage import AgentStore


MAX_HTTP_BODY = 64 * 1024
STATIC_FILES = {"/": "index.html", "/app.js": "app.js", "/feedback.js": "feedback.js", "/style.css": "style.css"}


class Runtime:
    def __init__(self, config: AppConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or Clock()
        self.store = AgentStore(config.database_path)
        self.provider = MockProvider(config.mock_database_path, self.clock, config.scenario_seed)
        self.provider.seed_demo_bookings()
        self.agent = ErrandAgent(config, self.store, self.provider, self.clock)
        self.monitor = ProactiveMonitor(self.agent)
        self._csrf: dict[str, str] = {}
        self._csrf_lock = threading.Lock()

    def csrf_token(self, session_id: str) -> str:
        with self._csrf_lock:
            if session_id not in self._csrf:
                self._csrf[session_id] = secrets.token_urlsafe(32)
            return self._csrf[session_id]

    def validate_csrf(self, session_id: str, value: str | None) -> bool:
        with self._csrf_lock:
            expected = self._csrf.get(session_id)
            return bool(expected and value and secrets.compare_digest(expected, value))

    def close(self) -> None:
        self.agent.close()


class LocalHTTPServer(ThreadingHTTPServer):
    # Drain handlers before Runtime.close() closes the shared SQLite stores.
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], runtime: Runtime) -> None:
        self.runtime = runtime
        self._request_semaphore = threading.BoundedSemaphore(8)
        super().__init__(address, LocalRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # ThreadingHTTPServer otherwise creates an unbounded thread per connection.
        self._request_semaphore.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_semaphore.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_semaphore.release()


class LocalRequestHandler(BaseHTTPRequestHandler):
    server: LocalHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "ErrandAgent/1.0"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # Requests can contain household details; diagnostics live in redacted traces.
        return

    def _secure_headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Cache-Control", "no-store")

    def _host_ok(self) -> bool:
        expected = f"{self.server.runtime.config.app_host}:{self.server.runtime.config.app_port}".casefold()
        supplied = (self.headers.get("Host") or "").casefold()
        if supplied != expected:
            self._send_json(421, {"error": "invalid_host", "message": "This app accepts requests only on its configured local address."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.casefold() != f"http://{expected}":
            self._send_json(403, {"error": "invalid_origin", "message": "Cross-origin requests are not accepted."})
            return False
        return True

    def _send_json(self, status: int, value: dict[str, Any], cookie: str | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._secure_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str) -> None:
        filename = STATIC_FILES.get(path)
        if not filename:
            self._send_json(404, {"error": "not_found", "message": "Not found."})
            return
        static_root = Path(__file__).resolve().parent / "web"
        try:
            data = (static_root / filename).read_bytes()
        except OSError:
            self._send_json(500, {"error": "static_missing", "message": "The local interface is unavailable."})
            return
        content_type = "text/html; charset=utf-8" if filename.endswith(".html") else "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
        self.send_response(200)
        self._secure_headers(content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session_id(self, create: bool = False) -> tuple[str | None, str | None]:
        cookie_header = self.headers.get("Cookie", "")
        parsed = SimpleCookie()
        try:
            parsed.load(cookie_header)
        except Exception:
            parsed = SimpleCookie()
        existing = parsed.get("errand_session")
        candidate = existing.value if existing else None
        session_id = self.server.runtime.agent.ensure_session(candidate) if create else candidate
        if not session_id:
            return None, None
        cookie = None
        if session_id != candidate:
            cookie = f"errand_session={session_id}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
        return session_id, cookie

    def _require_session(self) -> str | None:
        session_id, _cookie = self._session_id(False)
        if not session_id or not self.server.runtime.store.connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            self._send_json(401, {"error": "session_required", "message": "Reload the page to start a local session."})
            return None
        return session_id

    def _json_body(self) -> dict[str, Any] | None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "json_required", "message": "Send a JSON request body."})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_HTTP_BODY + 1
        if length <= 0 or length > MAX_HTTP_BODY:
            self._send_json(413, {"error": "body_limit", "message": "Request body is empty or too large."})
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json", "message": "Request body is not valid JSON."})
            return None
        if not isinstance(value, dict):
            self._send_json(400, {"error": "invalid_json", "message": "Request JSON must be an object."})
            return None
        return value

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        if parsed.path in STATIC_FILES:
            self._send_static(parsed.path)
            return
        runtime = self.server.runtime
        if parsed.path == "/api/session":
            session_id, cookie = self._session_id(create=True)
            if not session_id:
                self._send_json(500, {"error": "session_error", "message": "Could not start a local session."})
                return
            self._send_json(200, {
                "session_id": session_id, "csrf_token": runtime.csrf_token(session_id),
                "model": runtime.config.openai_model, "model_ready": runtime.config.live_model_ready,
                "llm_mode": runtime.config.llm_mode, "dev_controls": runtime.config.dev_controls,
                "budget": runtime.agent.budget.summary(),
            }, cookie)
            return
        session_id = self._require_session()
        if not session_id:
            return
        if parsed.path == "/api/history":
            rows = runtime.store.connection.execute(
                "SELECT id,role,content,created_at FROM messages WHERE session_id=? AND is_seed=0 ORDER BY created_at DESC LIMIT 100", (session_id,)
            ).fetchall()
            self._send_json(200, {"messages": [dict(row) for row in reversed(rows)], "current_options": runtime.agent.current_options(session_id)})
            return
        if parsed.path == "/api/bookings":
            self._send_json(200, {"bookings": runtime.agent.list_bookings(session_id)})
            return
        if parsed.path == "/api/approvals":
            self._send_json(200, {"approvals": runtime.agent.pending_approvals(session_id)})
            return
        if parsed.path == "/api/memories":
            self._send_json(200, {"memories": runtime.agent.memory_list(session_id)})
            return
        if parsed.path == "/api/traces":
            rows = runtime.store.connection.execute(
                "SELECT id,run_id,event,data_json,created_at FROM trace_events WHERE session_id=? ORDER BY id DESC LIMIT 120", (session_id,)
            ).fetchall()
            self._send_json(200, {"events": [
                {"id": row["id"], "run_id": row["run_id"], "event": row["event"], "data": json.loads(row["data_json"]), "created_at": row["created_at"]}
                for row in reversed(rows)
            ]})
            return
        if parsed.path == "/api/outbox":
            rows = runtime.store.connection.execute(
                "SELECT id,kind,content,created_at FROM outbox WHERE user_id='u_demo' AND status='pending' ORDER BY created_at LIMIT 50"
            ).fetchall()
            if rows:
                with runtime.store.transaction() as db:
                    db.executemany("UPDATE outbox SET status='delivered' WHERE id=?", [(row["id"],) for row in rows])
            self._send_json(200, {"notifications": [dict(row) for row in rows]})
            return
        if parsed.path.startswith("/api/providers/"):
            provider_id = unquote(parsed.path.removeprefix("/api/providers/"))
            if not provider_id.startswith("prov_") or len(provider_id) != 8:
                self._send_json(404, {"error": "not_found", "message": "Provider not found."})
                return
            try:
                provider = runtime.agent.provider_details(session_id, provider_id)
            except ApprovalError as error:
                self._send_json(403, {"error": error.code, "message": str(error)})
                return
            self._send_json(200, {"provider": provider})
            return
        self._send_json(404, {"error": "not_found", "message": "Not found."})

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        session_id = self._require_session()
        if not session_id:
            return
        if not self.server.runtime.validate_csrf(session_id, self.headers.get("X-CSRF-Token")):
            self._send_json(403, {"error": "csrf", "message": "Session token is missing or expired. Reload the page."})
            return
        body = self._json_body()
        if body is None:
            return
        runtime = self.server.runtime
        path = urlparse(self.path).path
        try:
            if path == "/api/chat":
                message = body.get("message")
                if not isinstance(message, str):
                    raise ValueError("Message must be text")
                result = runtime.agent.handle(session_id, message, str(body.get("client_message_id") or secrets.token_urlsafe(16)))
                self._send_json(200, result)
                return
            if path == "/api/select":
                result = runtime.agent.select_option(session_id, str(body.get("provider_id", "")), str(body.get("slot", "")))
                self._send_json(200, result)
                return
            if path == "/api/approve":
                result = runtime.agent.approve_action(session_id, str(body.get("action_id", "")), str(body.get("fingerprint", "")), str(body.get("request_id") or secrets.token_urlsafe(16)))
                self._send_json(200, result)
                return
            if path == "/api/reject":
                self._send_json(200, runtime.agent.reject_action(session_id, str(body.get("action_id", ""))))
                return
            if path == "/api/reschedule/select":
                result = runtime.agent.select_reschedule_slot(session_id, str(body.get("booking_id", "")), str(body.get("slot", "")))
                self._send_json(200, result)
                return
            if path == "/api/memory/correct":
                self._send_json(200, runtime.agent.correct_memory(session_id, str(body.get("memory_id", "")), body.get("value")))
                return
            if path == "/api/memory/forget":
                self._send_json(200, runtime.agent.forget_memory(session_id, str(body.get("memory_id", ""))))
                return
            if path == "/api/memory/confirm":
                self._send_json(200, runtime.agent.confirm_memory(session_id, str(body.get("memory_id", ""))))
                return
            if path == "/api/memory/reject":
                self._send_json(200, runtime.agent.reject_memory(session_id, str(body.get("memory_id", ""))))
                return
            if path.startswith("/api/demo/"):
                if not runtime.config.dev_controls:
                    self._send_json(404, {"error": "not_found", "message": "Not found."})
                    return
                if path == "/api/demo/scenario":
                    outcome = body.get("next_book")
                    if outcome not in {"success", "slot_taken", "timeout_booked", "timeout_not_booked"}:
                        raise ValueError("Unsupported deterministic booking outcome")
                    runtime.provider.force_next("book", outcome)
                    self._send_json(200, {"configured": outcome})
                    return
                if path == "/api/demo/cancel_provider":
                    booking_id = str(body.get("booking_id", ""))
                    runtime.provider.simulate_provider_cancellation(booking_id)
                    result = runtime.monitor.tick()
                    self._send_json(200, {"events": result})
                    return
            self._send_json(404, {"error": "not_found", "message": "Not found."})
        except ApprovalError as error:
            self._send_json(400, {"error": error.code, "message": str(error)})
        except MockToolError as error:
            self._send_json(400, {"error": error.code, "message": str(error)})
        except (ValueError, TypeError) as error:
            self._send_json(400, {"error": "invalid_request", "message": str(error)[:200]})
        except Exception:
            self._send_json(500, {"error": "internal_error", "message": "The local agent could not complete that request safely. Check the current action status before retrying."})


def _loopback_only(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_runtime(root: Path | None = None) -> Runtime:
    config = AppConfig.from_env(root)
    if not _loopback_only(config.app_host):
        raise ValueError("APP_HOST must resolve to a loopback address; this assignment app is local-only.")
    runtime = Runtime(config)
    runtime.agent.recover()
    runtime.monitor.tick()
    return runtime


def serve(runtime: Runtime) -> None:
    address = (runtime.config.app_host, runtime.config.app_port)
    server = LocalHTTPServer(address, runtime)
    stop_event = threading.Event()

    def poll_proactive() -> None:
        while not stop_event.wait(runtime.config.proactive_poll_seconds):
            runtime.monitor.tick()

    monitor_thread = threading.Thread(target=poll_proactive, name="errand-proactive-monitor", daemon=True)
    monitor_thread.start()
    base = f"http://{runtime.config.app_host}:{runtime.config.app_port}"
    print(f"Errand Agent running locally at {base}")
    print(f"Model: {runtime.config.openai_model} (ready={runtime.config.live_model_ready}); app API budget ${runtime.config.max_api_spend_usd:.2f}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        runtime.close()


def main() -> None:
    serve(build_runtime())


if __name__ == "__main__":
    main()
