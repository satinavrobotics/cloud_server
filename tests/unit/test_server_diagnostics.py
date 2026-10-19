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
        lines = [stamp(120, "request timed out talking to a"), stamp(60, "request timed out talking to b")]
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
        lines = [stamp(5, "request timed out"), stamp(5, "Traceback (most recent call last):")]
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
        return {"State": c["state"], "RestartCount": c.get("restarts", 0), "Config": {"Image": c.get("image", "img:1")},
                "HostConfig": {"Memory": c.get("limit", 0), "RestartPolicy": {"Name": c.get("policy", "")}}}

    def stats(self, cid):
        return {"usage_bytes": self.containers[cid].get("usage"), "engine_limit_bytes": 0}

    def logs(self, cid, tail, since=None):
        self.logged.append((cid, tail, since))
        return self.containers[cid].get("lines", [])[-tail:]


HOST_OK = {"mem_total_bytes": 4 << 30, "mem_available_bytes": 2 << 30, "swap_total_bytes": 0, "swap_used_bytes": 0,
           "disk_total_bytes": 100 << 30, "disk_free_bytes": 50 << 30, "load1": 0.5, "cpus": 4}


def make(containers, **kw):
    clock = {"t": NOW.timestamp()}
    kw.setdefault("host_reader", lambda: HOST_OK)
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
        assert out["summary"] == {"total": 3, "up": 1, "warn": 0, "down": 2, "checks_failing": 0}

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
        svc = DiagnosticsService(Flaky(FLEET), clock=lambda: NOW.timestamp(), host_reader=lambda: HOST_OK)
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
        diag_main.service = DiagnosticsService(FakeDocker(FLEET), clock=lambda: NOW.timestamp(), host_reader=lambda: HOST_OK)
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
                                            "restart_count", "started_at", "exit_code", "oom_killed", "memory", "severity",
                                            "findings_count"}
        assert set(body) >= {"host", "host_stats", "severity", "summary", "checks", "host_findings"}
        assert isinstance(body["host"], str) and body["summary"]["checks_failing"] == 0

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


# ---- memory, host and checks (added with the second round of diagnostics)
from packages.services.server_diagnostics.checks import (  # noqa: E402
    capability_check, gateway_checks, image_tag, run_checks, version_check)
from packages.services.server_diagnostics.host import host_findings, read_host  # noqa: E402


class TestMemory:
    def test_stats_are_the_working_set_without_reclaimable_cache(self, tmp_path):
        path = str(tmp_path / "docker.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(2)
        payload = json.dumps({"memory_stats": {"usage": 300_000_000, "limit": 400_000_000,
                                               "stats": {"inactive_file": 50_000_000}}}).encode()

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            conn.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            assert DockerReader(path, timeout=3).stats("abc") == {"usage_bytes": 250_000_000, "engine_limit_bytes": 400_000_000}
        finally:
            server.close()

    @pytest.mark.parametrize("usage,limit,expect", [
        (100, 1000, None), (790, 1000, None), (800, 1000, "memory-high"), (919, 1000, "memory-high"), (920, 1000, "memory-critical")])
    def test_thresholds(self, usage, limit, expect):
        state = running(mem_usage_bytes=usage * 1_000_000, mem_limit_bytes=limit * 1_000_000)
        ids = {f.id for f in analyze(state, [], NOW, WINDOW)}
        assert (ids & {"memory-high", "memory-critical"}) == ({expect} if expect else set())

    def test_no_limit_or_not_running_means_no_memory_finding(self):
        assert analyze(running(mem_usage_bytes=10**9, mem_limit_bytes=None), [], NOW, WINDOW) == []
        assert analyze(running(state="exited", exit_code=0, mem_usage_bytes=10**9, mem_limit_bytes=10**9), [], NOW, WINDOW) == []

    def test_the_overview_reports_usage_and_percent(self):
        containers = {"a": {"name": "x", "labels": {"com.docker.compose.service": "api"}, "state": STATE_OK,
                            "usage": 350 * 1048576, "limit": 384 * 1048576},
                      "b": {"name": "y", "labels": {"com.docker.compose.service": "db"}, "state": STATE_OK, "usage": 5 * 1048576}}
        svc, _ = make(containers)
        by = {s["name"]: s for s in svc.overview()["services"]}
        assert by["api"]["memory"]["percent"] == 91.1 and by["api"]["severity"] == "warn"
        assert by["db"]["memory"] == {"usage_bytes": 5 * 1048576, "limit_bytes": None, "percent": None}


class TestHost:
    def test_read_host_from_proc(self, tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 4000000 kB\nMemAvailable: 1000000 kB\nSwapTotal: 2000 kB\nSwapFree: 500 kB\n")
        load = tmp_path / "loadavg"
        load.write_text("1.50 1.00 0.50 1/100 123\n")
        h = read_host(str(meminfo), str(load), str(tmp_path))
        assert (h["mem_total_bytes"], h["mem_available_bytes"]) == (4_000_000 * 1024, 1_000_000 * 1024)
        assert (h["swap_total_bytes"], h["swap_used_bytes"]) == (2000 * 1024, 1500 * 1024) and h["load1"] == 1.5
        assert h["disk_total_bytes"] > 0 and h["cpus"] >= 1

    def test_unreadable_proc_gives_none(self, tmp_path):
        h = read_host(str(tmp_path / "no"), str(tmp_path / "no"), str(tmp_path / "no"))
        assert h["mem_total_bytes"] is None and h["load1"] is None and h["disk_total_bytes"] is None

    def test_findings(self):
        assert host_findings(HOST_OK) == []
        low = dict(HOST_OK, mem_available_bytes=300 << 20)
        assert [(f.id, f.severity) for f in host_findings(low)] == [("host-memory-low", "warn")]
        assert host_findings(dict(HOST_OK, mem_available_bytes=100 << 20))[0].severity == "error"
        assert host_findings(dict(HOST_OK, swap_total_bytes=1000, swap_used_bytes=800))[0].id == "host-swap-high"
        assert host_findings(dict(HOST_OK, disk_free_bytes=5 << 30))[0].id == "host-disk-low"
        assert host_findings(dict(HOST_OK, disk_free_bytes=2 << 30))[0].severity == "error"
        assert host_findings(dict(HOST_OK, load1=9.0))[0].id == "host-load-high"
        assert host_findings({}) == []


INFO_ALL = {"capabilities": {"agent": True, "scene_graph": True, "reconstruction": False, "semantic": False, "diagnostics": True},
            "livekit": {"available": True, "token_path": "/api/livekit-selfhost/createToken"}}


def fake_fetch(table):
    """table: url suffix -> status (or (status, body)); unknown paths answer 404; None = nothing answers."""
    def fetch(method, url):
        for suffix, answer in table.items():
            if url.endswith(suffix):
                if answer is None:
                    return None, "", "connection refused"
                status, body = answer if isinstance(answer, tuple) else (answer, "")
                return status, body, ""
        return 404, "", ""
    return fetch


GW = "http://gw"
OK_TABLE = {"/health": 200, "/api/v1/server-info": (200, json.dumps(INFO_ALL)), "/createToken": 405, "/api/agent/health": 200}


def svc(name, state="running", image="reg/sati/x:t1"):
    return {"name": name, "state": state, "image": image}


ALL_SERVICES = [svc("api-delegation-service"), svc("graph-builder-service"), svc("livekit-sfu-tokens"), svc("livekit-sfu", image="livekit/livekit:v1"),
                svc("agent-orchestrator-service"), svc("server-diagnostics")]


class TestChecks:
    def test_everything_fine(self):
        checks = run_checks(fake_fetch(OK_TABLE), GW, ALL_SERVICES)
        assert all(c.ok for c in checks), [c.detail for c in checks if not c.ok]
        assert {c.id for c in checks} >= {"gateway-answers", "gateway-reaches-api", "gateway-reaches-token-service",
                                           "gateway-reaches-agent", "capabilities-match", "same-version"}

    def test_the_token_route_502_is_the_raspberry_pi_bug(self):
        checks = run_checks(fake_fetch(dict(OK_TABLE, **{"/createToken": 502})), GW, ALL_SERVICES)
        bad = [c for c in checks if not c.ok]
        assert [c.id for c in bad] == ["gateway-reaches-token-service"] and bad[0].severity == "error"
        assert "tailnet address" in bad[0].hint and "502" in bad[0].detail

    def test_a_dead_gateway_stops_the_route_checks(self):
        checks = run_checks(fake_fetch({"/health": None}), GW, ALL_SERVICES)
        ids = [c.id for c in checks]
        assert ids[0] == "gateway-answers" and not checks[0].ok and "gateway-reaches-api" not in ids

    def test_api_502_and_404(self):
        for status in (502, 404):
            checks, info = gateway_checks(fake_fetch({"/health": 200, "/api/v1/server-info": status}), GW)
            assert info is None and [c.id for c in checks if not c.ok] == ["gateway-reaches-api"]

    def test_server_info_that_is_not_json(self):
        checks, info = gateway_checks(fake_fetch({"/health": 200, "/api/v1/server-info": (200, "<html>")}), GW)
        assert info is None and not checks[-1].ok

    def test_routes_are_only_probed_for_what_is_advertised(self):
        info = {"capabilities": {"agent": False}, "livekit": {"available": False}}
        checks, _ = gateway_checks(fake_fetch({"/health": 200, "/api/v1/server-info": (200, json.dumps(info))}), GW)
        assert {c.id for c in checks} == {"gateway-answers", "gateway-reaches-api"}

    def test_advertised_but_not_running(self):
        services = [s for s in ALL_SERVICES if s["name"] != "graph-builder-service"]
        c = capability_check(INFO_ALL, services)
        assert not c.ok and c.severity == "error" and "scene_graph is advertised but graph-builder-service is not on this server" in c.detail
        stopped = [dict(s, state="exited") if s["name"] == "livekit-sfu-tokens" else s for s in ALL_SERVICES]
        assert "not running" in capability_check(INFO_ALL, stopped).detail

    def test_running_but_not_advertised(self):
        info = {"capabilities": {"scene_graph": True, "agent": False, "reconstruction": False}, "livekit": {"available": False}}
        c = capability_check(info, [svc("graph-builder-service"), svc("reconstruction-service")])
        assert not c.ok and c.severity == "warn" and "reconstruction" in c.detail
        # graph-builder always runs, so scene_graph being off with it running is normal
        assert capability_check({"capabilities": {"scene_graph": False}, "livekit": {}}, [svc("graph-builder-service")]).ok

    def test_no_server_info_no_capability_check(self):
        assert capability_check(None, ALL_SERVICES) is None

    def test_version_skew(self):
        services = [svc("api-delegation-service", image="r/sati/api:old1"), svc("mission-dispatch", image="r/sati/m:new2"),
                    svc("server-diagnostics", image="r/sati/d:new2"), svc("postgres", image="postgres:17")]
        c = version_check(services)
        assert not c.ok and c.severity == "warn" and "old1: api-delegation-service" in c.detail and "new2: mission-dispatch" in c.detail
        assert "robot_compose.sh" in c.hint
        assert version_check([svc("api-delegation-service", image="a:latest"), svc("mission-dispatch", image="b:latest")]).ok
        assert version_check([]).ok

    @pytest.mark.parametrize("image,tag", [("a:1", "1"), ("host:5000/sati/x:abc", "abc"), ("host:5000/sati/x", "latest"),
                                           ("x:t@sha256:deadbeef", "t")])
    def test_image_tag(self, image, tag):
        assert image_tag(image) == tag

    def test_the_overview_includes_checks_and_the_worst_severity(self):
        containers = {"a": {"name": "x", "labels": {"com.docker.compose.service": "api-delegation-service"}, "state": STATE_OK}}
        svc_, _ = make(containers, gateway_url=GW, fetch=fake_fetch({"/health": None}))
        out = svc_.overview()
        assert out["summary"]["checks_failing"] >= 1 and out["severity"] == "error"
        assert out["checks"][0]["id"] == "gateway-answers" and out["host_stats"]["cpus"] == 4

    def test_host_findings_raise_the_overall_severity(self):
        low = dict(HOST_OK, disk_free_bytes=1 << 30)
        svc_, _ = make(FLEET, host_reader=lambda: low)
        out = svc_.overview()
        assert out["host_findings"][0]["id"] == "host-disk-low" and out["severity"] == "error"


# ---- the audit round (2026-10-19)
import time  # noqa: E402

from packages.services.server_diagnostics.service import _http_fetch  # noqa: E402


class TestRedactionHardening:
    @pytest.mark.parametrize("line,secret", [
        ("Cookie: session=abc123xyz", "abc123xyz"), ("Set-Cookie: sid=abcdef; Path=/", "abcdef"),
        ("Authorization: Token abcdef123456", "abcdef123456"), ("redis://:pw0rd@host:6379/0", "pw0rd"),
        ("curl -u admin:secret123 http://x", "secret123"), ("pass=hunter2", "hunter2"), ("passphrase=hunter2", "hunter2"),
        ("auth=abc123456", "abc123456"), ("key=abc123456", "abc123456"), ("sk=abc123456", "abc123456"),
        ("aws_secret_access_key wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"), ("password is hunter2 ok", "hunter2"),
        ('invalid password "hunter2"', "hunter2"), ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abc"),
        ("key sk-ant-abcdefghijklmnopqrstuvwxyz0123", "sk-ant"), ("-----BEGIN RSA PRIVATE KEY-----MIIEvQ", "MIIEvQ"),
        ("postgres://u:p@ss:word@h/db", "ss:word"), ("postgres://u:pass word@h", "pass word"),
        ("password='it\\'s secret'", "s secret"), ('password: "abc\\"def ghi"', "def ghi"),
        ("MINIO_ROOT_PASSWORD=abc", "=abc"), ("GET /x?token=abc123456 HTTP/1.1", "abc123456")])
    def test_more_shapes(self, line, secret):
        assert secret not in redact_line(line)

    @pytest.mark.parametrize("line", ["basic configuration loaded", "passed 3 checks", "the key is in the lock", "keyboard layout set",
                                      "INFO: GET /health 200 OK (3 tokens issued)"])
    def test_prose_is_untouched(self, line):
        assert redact_line(line) == line

    @pytest.mark.parametrize("payload", ["a-" * 8000, "a." * 8000, "password=" + "a" * 16000, "key" * 6000, "://" * 5000,
                                         "x-api-key-" * 3000, ("a" * 70 + "-") * 200])
    def test_hostile_lines_do_not_make_the_scrub_slow(self, payload):
        start = time.time()
        redact_line(payload)
        assert time.time() - start < 0.5


class TestAuditFixes:
    def test_level_fields_in_any_case_count_as_errors(self):
        lines = [stamp(5, 'level=error msg="db down"'), stamp(5, '{"level":"ERROR","msg":"x"}'), stamp(5, "found 0 errors, no error here")]
        (f,) = analyze(running(), lines, NOW, WINDOW)
        assert (f.id, f.count) == ("errors-logged", 2)

    def test_a_line_matching_two_rules_does_not_hide_other_errors(self):
        both = stamp(5, "ERROR connect() failed (111: Connection refused) while connecting to upstream")
        others = [stamp(5, "ERROR something else")] * 3
        ids = {f.id: f.count for f in analyze(running(), [both] + others, NOW, WINDOW)}
        assert ids["errors-logged"] == 3

    @pytest.mark.parametrize("line", ["request_timeout=30", "read timeout: 30", "connect_timeout=5s", "set timeout 10"])
    def test_timeout_settings_are_not_symptoms(self, line):
        assert analyze(running(), [stamp(5, line)], NOW, WINDOW) == []

    @pytest.mark.parametrize("line", ["request timed out", "upstream timeout while reading", "ETIMEDOUT", "context deadline exceeded"])
    def test_real_timeouts_are_found(self, line):
        assert "timeout" in {f.id for f in analyze(running(), [stamp(5, line)], NOW, WINDOW)}

    def test_log_text_is_split_on_newlines_only(self, tmp_path):
        path = str(tmp_path / "d.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        payload = frame(1, "2026-10-19T16:00:00Z one\x85 stale\x0cpart\r\n2026-10-19T16:00:01Z two\n")

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            conn.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            lines = DockerReader(path, timeout=3).logs("abc", 10)
        finally:
            server.close()
        assert lines == ["2026-10-19T16:00:00Z one\x85 stale\x0cpart", "2026-10-19T16:00:01Z two"]

    def test_a_non_json_answer_is_unavailable_not_a_crash(self, tmp_path):
        path = str(tmp_path / "d.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nnope!")
            conn.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            with pytest.raises(DockerUnavailable, match="not JSON"):
                DockerReader(path, timeout=3).inspect("abc")
        finally:
            server.close()

    def test_a_container_that_vanishes_mid_refresh_is_one_unknown_row_not_a_503(self):
        class Vanishing(FakeDocker):
            def inspect(self, cid):
                if cid == "b2":
                    raise DockerUnavailable("Docker answered 404 for /containers/b2/json")
                return super().inspect(cid)
        svc_ = DiagnosticsService(Vanishing(FLEET), clock=lambda: NOW.timestamp(), host_reader=lambda: HOST_OK)
        out = svc_.overview()
        by = {s["name"]: s for s in out["services"]}
        assert by["db"]["state"] == "unknown" and by["db"]["severity"] == "warn" and by["api"]["state"] == "running"
        assert out["summary"]["total"] == 3

    def test_a_service_stopped_by_hand_is_flagged(self):
        containers = {"a": {"name": "x", "labels": {"com.docker.compose.service": "w"},
                            "state": {"Status": "exited", "ExitCode": 0, "StartedAt": "2026-10-19T10:00:00Z"}, "policy": "unless-stopped"},
                      "b": {"name": "y", "labels": {"com.docker.compose.service": "job"},
                            "state": {"Status": "exited", "ExitCode": 0, "StartedAt": "2026-10-19T10:00:00Z"}, "policy": "no"}}
        by = {s["name"]: s for s in make(containers)[0].overview()["services"]}
        assert by["w"]["severity"] == "warn" and by["job"]["severity"] == "ok"

    def test_the_probe_does_not_follow_redirects(self):
        import http.server
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(301)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            status, _, err = _http_fetch("GET", f"http://127.0.0.1:{httpd.server_address[1]}/health", timeout=3)
        finally:
            httpd.shutdown()
        assert status == 301 and hits == ["/health"]

    @pytest.mark.parametrize("path", ["@evil.example/x", "//evil.example/x", "http://evil.example/x", ""])
    def test_a_server_supplied_token_path_cannot_redirect_the_probe(self, path):
        info = dict(INFO_ALL, livekit={"available": True, "token_path": path})
        seen = []

        def fetch(method, url):
            seen.append(url)
            return (200, json.dumps(info), "") if url.endswith("server-info") else (200 if url.endswith("/health") else 405, "", "")
        gateway_checks(fetch, GW)
        assert all(u.startswith(GW + "/") and "evil" not in u for u in seen)

    def test_overview_reads_containers_in_parallel_and_caps_log_reads(self):
        svc_, _ = make(FLEET)
        assert svc_._slots._value == 4
