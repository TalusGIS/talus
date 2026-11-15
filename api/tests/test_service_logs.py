"""The other services' error logs (decision-ai-substrate.md §13),
read bounded and scrubbed by allowlist. Every planted shape here is synthetic."""
import json

from app import service_logs as sl


def test_caddy_json_keeps_the_allowlist_and_drops_addresses_headers_and_query_strings():
    line = json.dumps({"level": "error", "ts": 1792593204.75, "logger": "http.log.error",
                       "msg": "dial tcp: lookup api on 127.0.0.11:53: no such host",
                       "request": {"remote_ip": "203.0.113.9", "client_ip": "203.0.113.9", "proto": "HTTP/2.0", "method": "GET",
                                   "host": "talus.example.org", "uri": "/api/v1/layers?token=abc123&x=1",
                                   "headers": {"Authorization": ["Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig"], "Cookie": ["s=1"]},
                                   "tls": {"server_name": "talus.example.org", "cipher_suite": 4865}},
                       "resp_headers": {"Set-Cookie": ["x"]}, "status": 502, "duration": 0.012})
    out = sl.scrub_caddy(line)
    d = json.loads(out)
    assert d["msg"].startswith("dial tcp") and d["status"] == 502 and d["logger"] == "http.log.error"
    assert d["request"] == {"method": "GET", "host": "talus.example.org", "uri": "/api/v1/layers"}
    assert "203.0.113.9" not in out and "Bearer" not in out and "Cookie" not in out and "token=" not in out
    assert "resp_headers" not in out and "proto" not in out


def test_caddy_non_json_passes_through_bounded():
    assert sl.scrub_caddy("x" * 900) == "x" * sl.LINE_CHARS


def test_postgres_keeps_error_lines_drops_statements_and_masks_hosts():
    keep = '2026-10-21 14:36:16.130 UTC [63421] FATAL:  password authentication failed for user "talus"'
    stmt = "2026-10-21 14:36:16.130 UTC [63421] STATEMENT:  UPDATE assets.parcels SET owner_name = 'J. Doe' WHERE id = 7"
    detail = '2026-10-21 14:36:16.131 UTC [63421] DETAIL:  Connection matched file "/var/lib/postgresql/data/pg_hba.conf" line 128'
    hosted = "2026-10-21 14:36:16.132 UTC [63422] LOG:  connection received: host=203.0.113.9 port=51234"
    assert sl.scrub_postgres(keep) == keep
    assert sl.scrub_postgres(stmt) is None
    assert sl.scrub_postgres(detail) is None
    assert sl.scrub_postgres(hosted) == "2026-10-21 14:36:16.132 UTC [63422] LOG:  connection received: host=*** port=51234"
    assert sl.scrub_postgres("not a postgres line at all") is None


def test_keycloak_keeps_warn_and_error_drops_frames_and_masks_identities():
    warn = "2026-10-21 14:36:16,130 WARN  [org.keycloak.events] (executor-thread-1) type=LOGIN_ERROR, realmId=talus, clientId=talus-portal, userId=5a1c, ipAddress=203.0.113.9, error=invalid_user_credentials, username=jdoe"
    err = "2026-10-21 14:36:17,001 ERROR [org.keycloak.connections.jpa] (main) Database connection terminated: FATAL: terminating connection due to administrator command (57P01)"
    info = "2026-10-21 14:36:17,002 INFO  [io.quarkus] (main) Keycloak 26.0.0 started in 9.8s"
    frame = "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(QueryExecutorImpl.java:2725)"
    more = "\t... 42 more"
    caused = "Caused by: org.postgresql.util.PSQLException: This connection has been closed."
    out = sl.scrub_keycloak(warn)
    assert "userId=***" in out and "ipAddress=***" in out and "username=***" in out
    assert "5a1c" not in out and "203.0.113.9" not in out and "jdoe" not in out
    assert "type=LOGIN_ERROR" in out and "error=invalid_user_credentials" in out
    assert sl.scrub_keycloak(err) == err
    assert sl.scrub_keycloak(info) is None
    assert sl.scrub_keycloak(frame) is None and sl.scrub_keycloak(more) is None
    assert sl.scrub_keycloak(caused) == caused


def test_tail_reads_only_the_window_and_only_complete_lines(tmp_path):
    f = tmp_path / "big.log"
    f.write_bytes(b"".join(f"line {i:06d} {'x' * 40}\n".encode() for i in range(5000)))   # ~250 KB
    got = sl.tail_lines(f, max_bytes=4096, n=10)
    assert len(got) == 10 and got[-1].startswith("line 004999") and all(l.startswith("line ") for l in got)


def test_read_reports_stdout_only_and_missing_sinks_as_not_available(tmp_path):
    out = {e["service"]: e for e in sl.read_service_logs(tmp_path)}
    assert set(out) == set(sl.SERVICES)
    assert out["openfga"]["note"] == sl.STDOUT_ONLY_NOTE and out["pg_tileserv"]["note"] == sl.STDOUT_ONLY_NOTE
    assert out["caddy"]["note"] == sl.UNAVAILABLE_NOTE and out["caddy"]["lines"] == []
    assert sl.read_service_logs(tmp_path / "nowhere")  # a missing root never raises


def test_read_picks_the_newest_postgres_file_and_scrubs_each_service(tmp_path):
    (tmp_path / "caddy").mkdir(); (tmp_path / "postgres").mkdir(); (tmp_path / "keycloak").mkdir()
    (tmp_path / "caddy" / "caddy.log").write_text(json.dumps({"level": "error", "msg": "boom", "request": {"remote_ip": "203.0.113.9", "uri": "/x?secret=1"}}) + "\n")
    old = tmp_path / "postgres" / "postgresql-Mon.log"; new = tmp_path / "postgres" / "postgresql-Tue.log"
    old.write_text("2026-10-20 10:00:00.000 UTC [1] ERROR:  old day\n"); new.write_text("2026-10-21 10:00:00.000 UTC [2] ERROR:  new day\n2026-10-21 10:00:00.001 UTC [2] STATEMENT:  SELECT secret FROM t\n")
    import os, time
    os.utime(old, (time.time() - 100, time.time() - 100))
    (tmp_path / "keycloak" / "keycloak.log").write_text("2026-10-21 10:00:00,000 INFO  [x] (m) started\n2026-10-21 10:00:01,000 WARN  [x] (m) username=jdoe locked out\n")
    out = {e["service"]: e for e in sl.read_service_logs(tmp_path)}
    assert out["postgres"]["file"] == "postgresql-Tue.log" and out["postgres"]["lines"] == ["2026-10-21 10:00:00.000 UTC [2] ERROR:  new day"]
    assert "203.0.113.9" not in json.dumps(out["caddy"]) and "secret=" not in json.dumps(out["caddy"])
    assert out["keycloak"]["lines"] == ["2026-10-21 10:00:01,000 WARN  [x] (m) username=*** locked out"]


def test_the_section_is_bounded_oldest_first(tmp_path):
    (tmp_path / "keycloak").mkdir()
    (tmp_path / "keycloak" / "keycloak.log").write_text("".join(f"2026-10-21 10:00:{i:02d},000 ERROR [x] (m) {'e' * 400} {i}\n" for i in range(50)))
    out = {e["service"]: e for e in sl.read_service_logs(tmp_path)}
    kc = out["keycloak"]
    assert len(json.dumps(sl.read_service_logs(tmp_path))) <= sl.SECTION_CHARS
    assert kc["note"] == "oldest lines dropped to fit the snapshot" and kc["lines"][-1].endswith(" 49")
