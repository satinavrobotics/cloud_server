"""Server diagnostics service (packages/services/server_diagnostics): redaction, log analysis, the read-only Docker
reader, the service that joins them, and its HTTP routes. No Docker is needed: the engine is a fake."""
import asyncio
import json
import socket
import struct
import threading
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from packages.services.server_diagnostics import main as diag_main
from packages.services.server_diagnostics.analyzer import analyze, line_time, recent_lines, worst
from packages.services.server_diagnostics.docker_api import DockerReader, DockerUnavailable, demultiplex
from packages.services.server_diagnostics.redact import MAX_LINE_CHARS, REDACTED, redact_line
from packages.services.server_diagnostics.service import DiagnosticsService, UnknownService

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 19, 16, 0, 0, tzinfo=timezone.utc)
WINDOW = 15 * 60


def stamp(seconds_ago: float, text: str) -> str:
    return f"{(NOW - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%dT%H:%M:%S')}.123456789Z {text}"


def running(**over):
    state = {"state": "running", "health": "healthy", "restart_count": 0, "exit_code": None, "oom_killed": False,
             "started_at": NOW - timedelta(days=2)}
    state.update(over)
    return state


class TestRedact:
    @pytest.mark.parametrize("line,secret", [
        ("login password=hunter2 ok", "hunter2"),
        ('{"api_key": "abcd1234efgh"}', "abcd1234efgh"),
        ("SECRET_KEY: s3cr3tvalue", "s3cr3tvalue"),
        ("MINIO_SECRET_KEY='quoted secret'", "quoted secret"),
        ("connecting to postgres://sati:p4ssw0rd@db:5432/sati", "p4ssw0rd"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig-nature_123 issued", "eyJhbGciOiJIUzI1NiJ9"),
        ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
    ])
    def test_secrets_are_removed(self, line, secret):
        out = redact_line(line)
        assert secret not in out and REDACTED in out

    def test_ordinary_lines_are_untouched(self):
        line = "INFO: 100.116.20.49:40034 - GET /health HTTP/1.1 200 OK (tokens issued: 3)"
        assert redact_line(line) == line

    def test_the_url_keeps_its_user_and_host(self):
        assert redact_line("postgres://sati:pw@db/x") == f"postgres://sati:{REDACTED}@db/x"

    def test_long_lines_are_cut(self):
        out = redact_line("x" * (MAX_LINE_CHARS + 50))
        assert len(out) < MAX_LINE_CHARS + 20 and out.endswith("...[cut]")


class TestAnalyzer:
    def test_a_quiet_healthy_service_has_no_findings(self):
        lines = [stamp(60, "INFO: GET /health 200")]
        assert analyze(running(), lines, NOW, WINDOW) == []
        assert worst([]) == "ok"

    def test_connection_refused_is_the_gateway_bug_of_the_pi(self):
        line = stamp(30, "[error] 7#7: *1 connect() failed (111: Connection refused) while connecting to upstream")
        findings = analyze(running(), [line], NOW, WINDOW)
        assert {f.id for f in findings} >= {"connection-refused", "upstream-down"}
        assert worst(findings) == "error"
        top = next(f for f in findings if f.id == "connection-refused")
        assert top.count == 1 and "connect() failed" in top.sample and "listens" in top.hint

    def test_old_lines_are_ignored_so_a_fixed_problem_goes_away(self):
        old = stamp(WINDOW + 600, "ERROR connection refused")
        assert analyze(running(), [old], NOW, WINDOW) == []

    def test_counts_and_newest_sample(self):
        lines = [stamp(120, "timeout talking to a"), stamp(60, "timeout talking to b")]
        (f,) = analyze(running(), lines, NOW, WINDOW)
        assert (f.id, f.severity, f.count) == ("timeout", "warn", 2) and f.sample.endswith("to b")

    @pytest.mark.parametrize("text,rule", [
        ("Traceback (most recent call last):", "traceback"),
        ("psycopg2.OperationalError: could not connect to server", "database"),
        ("FATAL: password authentication failed for user", "auth-failed"),
        ("OSError: [Errno 28] No space left on device", "disk-full"),
        ("OSError: [Errno 98] Address already in use", "port-in-use"),
        ("socket.gaierror: Name or service not known", "name-resolution"),
        ("MemoryError", "out-of-memory"),
        ("mqtt client disconnected unexpectedly", "mqtt"),
        ("open() failed: Permission denied", "permission-denied"),
    ])
    def test_the_known_failures(self, text, rule):
        assert rule in {f.id for f in analyze(running(), [stamp(10, text)], NOW, WINDOW)}

    def test_routine_info_lines_that_mention_a_timeout_are_not_symptoms(self):
        lines = [stamp(5, "2026-10-19 17:18:35,060 - GraphBuilderService - INFO -    Image buffer timeout: 30.0s"),
                 stamp(5, "[info] reconnect timeout set to 5s")]
        assert analyze(running(), lines, NOW, WINDOW) == []
        assert "timeout" in {f.id for f in analyze(running(), [stamp(5, "ERROR request timeout after 30s")], NOW, WINDOW)}

    def test_error_level_lines_warn_and_many_escalate(self):
        few = [stamp(10, "ERROR something odd")] * 3
        (f,) = analyze(running(), few, NOW, WINDOW)
        assert (f.id, f.severity, f.count) == ("errors-logged", "warn", 3)
        many = [stamp(10, "ERROR something odd")] * 25
        assert analyze(running(), many, NOW, WINDOW)[0].severity == "error"

    def test_an_error_line_already_explained_by_a_rule_is_not_counted_twice(self):
        line = stamp(10, "ERROR connection refused by broker")
        assert [f.id for f in analyze(running(), [line], NOW, WINDOW)] == ["connection-refused"]

    @pytest.mark.parametrize("state,expect", [
        (running(oom_killed=True, state="exited", exit_code=137), {"oom-killed", "stopped-with-error"}),
        (running(state="restarting", restart_count=4, exit_code=1), {"crash-loop"}),
        (running(state="exited", exit_code=2), {"stopped-with-error"}),
        (running(state="paused"), {"paused"}),
        (running(health="unhealthy"), {"unhealthy"}),
        (running(restart_count=2, started_at=NOW - timedelta(minutes=2)), {"restarted-recently"}),
    ])
    def test_container_state_problems(self, state, expect):
        findings = analyze(state, [], NOW, WINDOW)
        assert {f.id for f in findings} == expect and worst(findings) in ("warn", "error")

    def test_a_job_that_finished_cleanly_is_fine(self):
        assert analyze(running(state="exited", exit_code=0, health=None), [], NOW, WINDOW) == []

    def test_old_restarts_do_not_warn_forever(self):
        assert analyze(running(restart_count=5), [], NOW, WINDOW) == []

    def test_errors_sort_before_warnings(self):
        lines = [stamp(5, "timeout"), stamp(5, "Traceback (most recent call last):")]
        severities = [f.severity for f in analyze(running(), lines, NOW, WINDOW)]
        assert severities == sorted(severities, key=lambda s: -{"warn": 1, "error": 2}[s])

    def test_line_time_and_window(self):
        assert line_time("2026-10-19T16:00:00.5Z hi") == NOW
        assert line_time("no timestamp") is None
        kept = recent_lines([stamp(10, "a"), stamp(WINDOW + 5, "b"), "continuation"], NOW, WINDOW)
        assert [k.rsplit(" ", 1)[-1] for k in kept] == ["a", "continuation"]


def frame(stream: int, text: str) -> bytes:
    data = text.encode()
    return struct.pack(">BxxxL", stream, len(data)) + data


class TestDockerReader:
    def test_demultiplex_joins_both_streams(self):
        raw = frame(1, "out line\n") + frame(2, "err line\n")
        assert demultiplex(raw).splitlines() == ["out line", "err line"]

    def test_a_tty_stream_is_plain_text(self):
        assert demultiplex(b"plain text\nline two\n").splitlines() == ["plain text", "line two"]

    def test_a_truncated_frame_keeps_what_was_whole(self):
        raw = frame(1, "whole\n") + struct.pack(">BxxxL", 1, 999) + b"cut"
        assert demultiplex(raw).splitlines() == ["whole"]

    def test_unreachable_socket(self, tmp_path):
        reader = DockerReader(str(tmp_path / "missing.sock"), timeout=1)
        with pytest.raises(DockerUnavailable, match="cannot reach the Docker socket"):
            reader.list_containers()

    def test_it_only_ever_sends_get(self, tmp_path):
        """A real unix socket server records what the reader sends: GET only, and the right paths."""
        path = str(tmp_path / "docker.sock")
        seen = []
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(5)

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                request = conn.recv(4096).decode()
                seen.append(request.split("\r\n")[0])
                if "/logs" in request:
                    body = frame(1, "2026-10-19T16:00:00Z hello\n")
                elif "/json?all" in request:
                    body = json.dumps([{"Id": "abc"}]).encode()
                else:
                    body = json.dumps({"State": {}}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        try:
            reader = DockerReader(path, timeout=3)
            assert reader.list_containers() == [{"Id": "abc"}]
            assert reader.inspect("abc") == {"State": {}}
            assert reader.logs("abc", 5, since=100) == ["2026-10-19T16:00:00Z hello"]
        finally:
            server.close()
        assert all(line.startswith("GET ") for line in seen) and len(seen) == 3
        assert "tail=5" in seen[2] and "since=100" in seen[2] and "timestamps=1" in seen[2]


class FakeDocker:
    """Containers keyed by id: {labels, name, inspect, lines}."""

    def __init__(self, containers):
        self.containers = containers
        self.logged = []

    def list_containers(self):
        return [{"Id": cid, "Names": ["/" + c["name"]], "Labels": c.get("labels", {})} for cid, c in self.containers.items()]

    def inspect(self, cid):
        c = self.containers[cid]
        return {"State": c["state"], "RestartCount": c.get("restarts", 0), "Config": {"Image": c.get("image", "img:1")}}

    def logs(self, cid, tail, since=None):
        self.logged.append((cid, tail, since))
        return self.containers[cid].get("lines", [])[-tail:]


def make(containers, **kw):
    clock = {"t": NOW.timestamp()}
    svc = DiagnosticsService(FakeDocker(containers), clock=lambda: clock["t"], **kw)
    return svc, clock


STATE_OK = {"Status": "running", "Health": {"Status": "healthy"}, "StartedAt": "2026-10-17T10:00:00.1Z", "ExitCode": 0}

FLEET = {
    "a1": {"name": "docker_compose-api-1", "labels": {"com.docker.compose.service": "api"}, "state": STATE_OK,
           "lines": [stamp(20, "INFO ok")]},
    "b2": {"name": "docker_compose-db-1", "labels": {"com.docker.compose.service": "db"},
           "state": {"Status": "exited", "ExitCode": 1, "StartedAt": "2026-10-19T10:00:00Z"}},
    "c3": {"name": "sati_nginx_gateway", "state": dict(STATE_OK),
           "lines": [stamp(5, "[error] connect() failed (111: Connection refused) while connecting to upstream")]},
    "d4": {"name": "unrelated_thing", "state": STATE_OK},
}


class TestService:
    def test_overview_lists_this_servers_containers_with_severity(self):
        svc, _ = make(FLEET)
        out = svc.overview()
        assert [s["name"] for s in out["services"]] == ["api", "db", "sati_nginx_gateway"]    # 'unrelated_thing' is not ours
        by = {s["name"]: s for s in out["services"]}
        assert by["api"]["severity"] == "ok" and by["api"]["status_text"] == "running (healthy)"
        assert by["db"]["severity"] == "error" and by["db"]["status_text"] == "exited, exit code 1"
        assert by["sati_nginx_gateway"]["severity"] == "error" and by["sati_nginx_gateway"]["findings_count"] == 2
        assert out["summary"] == {"total": 3, "up": 1, "warn": 0, "down": 2}

    def test_overview_is_cached_for_a_few_seconds(self):
        svc, clock = make(FLEET)
        first = svc.overview()
        svc._docker.logged.clear()
        assert svc.overview() is first and svc._docker.logged == []
        clock["t"] += 6
        svc.overview()
        assert svc._docker.logged

    def test_logs_are_redacted_and_bounded(self):
        containers = {"a1": {"name": "x", "labels": {"com.docker.compose.service": "api"}, "state": STATE_OK,
                             "lines": [stamp(1, f"line {i} password=hunter2") for i in range(50)]}}
        svc, _ = make(containers)
        out = svc.logs("api", 10)
        assert len(out["lines"]) == 10 and out["redacted"] is True and out["truncated"] is True
        assert all("hunter2" not in line for line in out["lines"]) and out["lines"][-1].endswith("[redacted]")

    def test_logs_clamp_the_line_count(self):
        svc, _ = make(FLEET)
        svc.logs("api", 99999)
        assert svc._docker.logged[-1][1] == 2000

    def test_a_service_can_be_named_by_its_container(self):
        svc, _ = make(FLEET)
        assert svc.logs("docker_compose-api-1", 5)["name"] == "api"

    def test_unknown_service(self):
        svc, _ = make(FLEET)
        with pytest.raises(UnknownService):
            svc.logs("nope")
        with pytest.raises(UnknownService):
            svc.analysis("../../etc/passwd")

    def test_analysis_returns_findings(self):
        svc, _ = make(FLEET)
        out = svc.analysis("sati_nginx_gateway")
        assert out["severity"] == "error" and out["scanned_lines"] == 1
        assert {f["id"] for f in out["findings"]} == {"connection-refused", "upstream-down"}

    def test_analysis_reads_only_the_recent_window(self):
        svc, clock = make(FLEET, window_minutes=10)
        svc.analysis("api")
        assert svc._docker.logged[-1][2] == int(clock["t"] - 600)

    def test_replicas_keep_their_container_names(self):
        containers = {
            "a": {"name": "p-w-1", "labels": {"com.docker.compose.service": "w"}, "state": STATE_OK},
            "b": {"name": "p-w-2", "labels": {"com.docker.compose.service": "w"}, "state": STATE_OK}}
        svc, _ = make(containers)
        assert [s["name"] for s in svc.overview()["services"]] == ["p-w-1", "p-w-2"]

    def test_the_include_pattern_replaces_the_default_filter(self):
        svc, _ = make(FLEET, include="^unrelated")
        assert [s["name"] for s in svc.overview()["services"]] == ["unrelated_thing"]

    def test_a_log_failure_does_not_hide_the_state(self):
        class Flaky(FakeDocker):
            def logs(self, cid, tail, since=None):
                raise DockerUnavailable("boom")
        svc = DiagnosticsService(Flaky(FLEET), clock=lambda: NOW.timestamp())
        assert svc.overview()["summary"]["total"] == 3


class Client:
    """The app called in-process over ASGI (the lifespan is not run: each test sets the service itself)."""

    def request(self, method, path):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=diag_main.app), base_url="http://t") as c:
                return await c.request(method, path)
        return asyncio.run(go())

    def get(self, path):
        return self.request("GET", path)

    def __getattr__(self, method):
        return lambda path: self.request(method.upper(), path)


class TestRoutes:
    @pytest.fixture
    def client(self):
        diag_main.service = DiagnosticsService(FakeDocker(FLEET), clock=lambda: NOW.timestamp())
        yield Client()
        diag_main.service = None

    def test_not_initialized(self):
        diag_main.service = None
        for path in ("/services", "/services/api/logs", "/services/api/analysis"):
            assert Client().get(path).status_code == 503

    def test_services(self, client):
        body = client.get("/services").json()
        assert body["summary"]["total"] == 3 and {"generated_at", "host", "services"} <= set(body)
        assert set(body["services"][0]) == {"name", "container", "image", "state", "health", "status_text",
                                            "restart_count", "started_at", "exit_code", "oom_killed", "severity",
                                            "findings_count"}

    def test_logs_and_analysis(self, client):
        logs = client.get("/services/api/logs?lines=5").json()
        assert set(logs) == {"name", "lines", "truncated", "redacted"}
        analysis = client.get("/services/sati_nginx_gateway/analysis").json()
        assert set(analysis) == {"name", "severity", "scanned_lines", "findings"}
        assert set(analysis["findings"][0]) == {"id", "severity", "title", "detail", "hint", "count", "sample"}

    def test_bad_input(self, client):
        assert client.get("/services/nope/logs").status_code == 404
        assert client.get("/services/nope/analysis").status_code == 404
        assert client.get("/services/api/logs?lines=0").status_code == 422
        assert client.get("/services/api/logs?lines=2001").status_code == 422

    def test_only_get_is_allowed(self, client):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)("/services/api/logs").status_code == 405

    def test_docker_unreachable_is_a_503_with_the_reason(self, client):
        diag_main.service = DiagnosticsService(DockerReader("/nonexistent.sock", timeout=1))
        for path in ("/services", "/services/api/logs", "/services/api/analysis"):
            r = client.get(path)
            assert r.status_code == 503 and "Docker socket" in json.dumps(r.json())

    def test_health_and_root(self, client):
        assert client.get("/health").status_code == 200
        assert "services" in client.get("/").json()["endpoints"]
