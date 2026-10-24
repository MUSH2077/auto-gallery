#!/usr/bin/env python3
"""Opt-in host controller for idle download workers (foreground; supervise it).

Default Compose/deploy remains resident. Run this on a Linux/WSL Docker host alongside
an already deployed application. SIGINT/SIGTERM restores resident downloads;
after an unclean exit, a host service manager must restart this controller.
The controller never holds a download/DB lock or changes a job's state.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import socket
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import uuid
from urllib.parse import urlparse, unquote


class UnsupportedDockerEndpoint(RuntimeError):
    """The configured Docker endpoint cannot use the local socket bridge."""


def _context_docker_host(docker):
    """Return the effective local Docker Unix socket, or None for remote endpoints."""
    explicit_context = os.environ.get("DOCKER_CONTEXT")
    explicit_host = os.environ.get("DOCKER_HOST")
    if explicit_context:
        current = explicit_context
    elif explicit_host:
        value = explicit_host
        parsed = urlparse(value)
        return unquote(parsed.path) if parsed.scheme == "unix" and parsed.path else None
    else:
        try:
            current = subprocess.run([docker, "context", "show"], capture_output=True,
                text=True, timeout=10, check=True).stdout.strip() or "default"
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        value = subprocess.run([docker, "context", "inspect", current,
            "--format", "{{json .Endpoints.docker.Host}}"], capture_output=True,
            text=True, timeout=10, check=True).stdout.strip()
        value = json.loads(value) if value else ""
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "unix" or not parsed.path:
        return None
    return unquote(parsed.path)


def _UnixHTTPConnection(socket_path):
    """Create one HTTP connection while keeping http.client off the CLI path."""
    import http.client

    class UnixHTTPConnection(http.client.HTTPConnection):
        def __init__(self, path):
            super().__init__("localhost", timeout=10)
            self.socket_path = path

        def connect(self):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock = sock
            try:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
            except Exception:
                sock.close()
                self.sock = None
                raise

    return UnixHTTPConnection(socket_path)


class UnixDockerBridge:
    """Run the Redis bridge through a local Docker Engine exec/attach stream."""

    def __init__(self, docker, backend_id, token):
        path = _context_docker_host(docker)
        if not path:
            raise UnsupportedDockerEndpoint("Docker endpoint is not a local Unix socket")
        self.socket_path = path
        self.buffer = b""
        self.stdout_buffer = b""
        self.messages = []
        self.work_hint = False
        version = self._json_request("GET", "/version")
        api_version = os.environ.get("DOCKER_API_VERSION") or version.get("ApiVersion")
        if not api_version:
            raise UnsupportedDockerEndpoint("Docker API version is unavailable")
        created = self._json_request("POST", f"/v{api_version}/containers/{backend_id}/exec",
            {"AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
             "Tty": False, "Cmd": ["python", "-u", "-m", "app.services.download_runtime", token]})
        exec_id = created.get("Id")
        if not exec_id:
            raise RuntimeError("Docker exec did not return an id")
        try:
            self.sock = self._connect()
            self._start_stream(f"/v{api_version}/exec/{exec_id}/start")
        except Exception:
            if getattr(self, "sock", None) is not None:
                self.sock.close()
            raise

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(self.socket_path)
        except Exception:
            sock.close()
            raise
        return sock

    def _send_request(self, method, path, payload=None, upgrade=False):
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = [f"{method} {path} HTTP/1.1", "Host: docker", "Content-Type: application/json",
                   f"Content-Length: {len(body)}", "Connection: Upgrade" if upgrade else "Connection: close"]
        if upgrade:
            headers.append("Upgrade: tcp")
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + body)

    def _json_request(self, method, path, payload=None):
        connection = _UnixHTTPConnection(self.socket_path)
        try:
            body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
            connection.request(method, path, body=body,
                headers={"Host": "docker", "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Docker Engine HTTP {response.status}")
            return json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def _read_http(self):
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("Docker Engine closed the control connection")
            response += chunk
        header, _, body = response.partition(b"\r\n\r\n")
        lines = header.decode("latin1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
        return status, headers, body

    def _start_stream(self, path):
        self._send_request("POST", path, {"Detach": False, "Tty": False}, upgrade=True)
        status, _headers, body = self._read_http()
        if status not in (101, 200):
            raise RuntimeError(f"Docker exec attach HTTP {status}")
        self.buffer = body

    def _parse_frames(self):
        while True:
            if len(self.buffer) < 8:
                return
            stream = self.buffer[0]
            size = int.from_bytes(self.buffer[4:8], "big")
            if len(self.buffer) < 8 + size:
                return
            payload = self.buffer[8:8 + size]
            self.buffer = self.buffer[8 + size:]
            if stream == 1:
                self.stdout_buffer += payload
                while b"\n" in self.stdout_buffer:
                    line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
                    if line:
                        self.messages.append(json.loads(line))

    def receive(self, timeout):
        if not self.messages:
            self._parse_frames()
        if not self.messages:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("Redis bridge exited")
            self.buffer += chunk
            self._parse_frames()
        if self.messages:
            value = self.messages.pop(0)
            if value.get("event") == "work":
                self.work_hint = True
            return value
        return None

    def send(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.sock.settimeout(3)
        try:
            while self.sock.recv(65536):
                pass
        except (OSError, socket.timeout):
            pass
        finally:
            self.sock.close()


class BridgeProcess:
    def __init__(self, docker, backend_id, token, transport="cli"):
        self.transport = "cli"
        self.process = None
        self.stream = None
        if transport == "unix":
            try:
                self.stream = UnixDockerBridge(docker, backend_id, token)
                self.transport = "unix"
            except UnsupportedDockerEndpoint:
                # Remote contexts and unsupported endpoints keep the old CLI path.
                self.stream = None
        if self.stream is None:
            self.process = subprocess.Popen([docker, "exec", "-i", backend_id, "python", "-u", "-m",
                "app.services.download_runtime", token], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        self.buffer = b""
        self.messages = []
        self.work_hint = False
        self.sequence = 0

    def receive(self, timeout):
        if self.stream is not None:
            value = self.stream.receive(timeout)
            if value and value.get("event") == "work":
                self.work_hint = True
            return value
        if not self.messages:
            ready, _, _ = select.select([self.process.stdout], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Redis bridge exited")
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.messages.append(json.loads(line))
        if self.messages:
            value = self.messages.pop(0)
            if value.get("event") == "work":
                self.work_hint = True
            return value
        return None

    def command(self, action, **arguments):
        self.sequence += 1
        wanted = self.sequence
        request = dict(id=wanted, action=action, **arguments)
        data = (json.dumps(request) + "\n").encode()
        if self.stream is not None:
            self.stream.send(data)
        else:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        end = time.monotonic() + 7
        while time.monotonic() < end:
            value = self.receive(max(0, end-time.monotonic()))
            if value and value.get("id") == wanted:
                if value.get("error"):
                    raise RuntimeError("Redis bridge command failed: " + value["error"])
                return value["value"]
        raise TimeoutError("Redis bridge response timeout")

    def wait(self, seconds):
        if self.work_hint:
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.receive(max(0, end-time.monotonic()))
            if self.work_hint:
                break

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
            return
        if self.process is None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def native_wsl_binds(config, distro):
    """Translate Windows-rendered bind paths for the native WSL Docker client."""
    result=json.loads(json.dumps(config));changed=False
    for service in result.get('services',{}).values():
        for volume in service.get('volumes',[]):
            if not isinstance(volume,dict) or volume.get('type')!='bind':continue
            source=volume.get('source','')
            for host in ('wsl$','wsl.localhost'):
                prefix=chr(92)*2+host+chr(92)+distro+chr(92)
                if source.lower().startswith(prefix.lower()):
                    volume['source']='/'+source[len(prefix):].replace(chr(92),'/')
                    changed=True
    return result,changed


def process_memory():
    """Host controller plus its CLI children; bridge is already in API cgroup."""
    todo = [os.getpid()]
    rows = []
    while todo:
        pid = todo.pop()
        try:
            root = Path("/proc") / str(pid)
            stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
            children = root / "task" / str(pid) / "children"
            todo.extend(map(int, children.read_text().split()))
            values = dict(line.split(":", 1) for line in (root / "smaps_rollup").read_text().splitlines() if ":" in line)
            kb = lambda key: int(values.get(key, "0 kB").split()[0]) * 1024
            rows.append(dict(pid=pid, starttime_ticks=int(stat[19]), pss_bytes=kb("Pss"),
                uss_bytes=kb("Private_Clean")+kb("Private_Dirty"),
                cpu_s=(int(stat[11])+int(stat[12])) / os.sysconf("SC_CLK_TCK")))
        except (OSError, ValueError, KeyError):
            continue
    return dict(host_pss_bytes=sum(x["pss_bytes"] for x in rows),
                host_uss_bytes=sum(x["uss_bytes"] for x in rows),
                host_cpu_s=sum(x["cpu_s"] for x in rows), children_metrics=rows)


def demand(snapshot, lead_seconds):
    due = snapshot.get("next_due_in_s")
    return bool(snapshot["runnable"] or snapshot["inflight"] or snapshot["deferred"] or
                (due is not None and due <= lead_seconds))


class Controller:
    def __init__(self, args):
        self.args = args
        self.token = uuid.uuid4().hex
        self.bridge = None
        self.owned = False
        self.stopping = False
        self.state = "awake"
        self.drain_token = None
        self.last_work = time.monotonic()
        self.last_transition = time.time()
        self.ready_reported = False
        self.health_checked_at = 0.0

    def docker(self, *args):
        result = subprocess.run([self.args.docker, *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            # Never echo Compose configuration or environment in error messages.
            raise RuntimeError("Docker operation failed: " + args[0])
        return result.stdout.strip()

    def compose(self, *args):
        path=self.args.compose_file
        temporary=None
        try:
            # The reusable Windows harness emits resolved JSON with UNC binds.
            # Native WSL Compose otherwise treats them as relative Linux paths.
            if args and args[0]=='up' and path.suffix=='.json' and os.environ.get('WSL_DISTRO_NAME') and not self.args.docker.lower().endswith('.exe'):
                config,changed=native_wsl_binds(json.loads(path.read_text()),os.environ['WSL_DISTRO_NAME'])
                if changed:
                    with tempfile.NamedTemporaryFile(mode='w',suffix='.json',prefix='ag-download-',delete=False) as file:
                        json.dump(config,file);temporary=Path(file.name)
                    path=temporary
            return self.docker("compose", "--project-directory", str(self.args.compose_file.parent), "-p", self.args.project, "-f", str(path), *args)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)

    def container(self, service):
        ids = self.compose("ps", "-a", "-q", service).splitlines()
        if len(ids) > 1:
            raise RuntimeError("Expected exactly one container for " + service)
        return ids[0] if ids else None

    def emit(self, event, **extra):
        value = dict(event=event, state=self.state, wall_unix_s=time.time(),
                     monotonic_s=time.monotonic(), pid=os.getpid(), token=self.token,
                     transport=getattr(self.bridge, "transport", None),
                     last_transition_unix_s=self.last_transition, **extra, **process_memory())
        print(json.dumps(value, separators=(",", ":")), flush=True)
        if self.args.status_file:
            path = self.args.status_file
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(value) + "\n")
            temporary.replace(path)

    def transition(self, state, event, **extra):
        self.state = state
        self.last_transition = time.time()
        self.emit(event, **extra)

    def start(self):
        self.transition("starting", "start_requested")
        container = self.container("worker-download")
        if container:
            self.docker("start", container)
        else:
            self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "worker-download")
        self.ready_reported = False
        self.last_work = time.monotonic()
        self.transition("awake", "container_started")

    def cancel(self):
        self.bridge.command("cancel")
        self.drain_token = None
        self.last_work = time.monotonic()
        self.transition("awake", "drain_cancelled")

    def step(self, snapshot, hint=False):
        if self.state == "awake" and time.monotonic() - self.health_checked_at >= 30:
            container = self.container("worker-download")
            running = bool(container and self.docker("inspect", "--format", "{{.State.Running}}", container) == "true")
            self.health_checked_at = time.monotonic()
            if not running:
                self.start()
                return
        needed = demand(snapshot, self.args.start_lead_seconds)
        if needed or hint:
            self.last_work = time.monotonic()
            if self.state == "stopped":
                self.start()
            elif self.state == "draining":
                self.cancel()
            return
        if self.state == "stopped":
            return
        supervisor = snapshot.get("supervisor", {})
        if self.state == "draining":
            # Renew and read the acknowledgement atomically relative to Redis
            # publication. Only worker exit (not an empty queue) authorizes stop.
            # Resolve the immutable container ID before refreshing the stop
            # authorization; a slow Docker lookup must not consume its lease.
            container = self.container("worker-download")
            snapshot = self.bridge.command("drain", drain_token=self.drain_token)
            if demand(snapshot, self.args.start_lead_seconds):
                self.cancel()
                return
            supervisor = snapshot.get("supervisor", {})
            if supervisor.get("drain_token") == self.drain_token and supervisor.get("drain_ack") is True:
                self.emit("drain_acknowledged", drain_token=self.drain_token)
                if container:
                    # Consumers and their RQ schedulers have already exited.
                    self.docker("stop", "--time", "5", container)
                self.bridge.command("cancel")
                self.drain_token = None
                self.transition("stopped", "stopped")
            return
        if time.monotonic() - self.last_work >= self.args.idle_seconds:
            self.drain_token = self.token + ":" + uuid.uuid4().hex
            self.bridge.command("drain", drain_token=self.drain_token)
            self.transition("draining", "drain_requested", drain_token=self.drain_token)
        elif not self.ready_reported and supervisor.get("worker_count", 0) >= supervisor.get("expected_worker_count", 1) and not supervisor.get("drain_token"):
            self.ready_reported = True
            self.emit("ready", readiness_basis="supervisor_count_report; dispatch does not wait for this event")

    def connect(self):
        backend = self.container("backend")
        if not backend:
            raise RuntimeError("Start the application backend before the lifecycle controller")
        container = self.container("worker-download")
        if container:
            environment = json.loads(self.docker("inspect", "--format", "{{json .Config.Env}}", container))
            enabled = "DOWNLOAD_WORKER_LIFECYCLE=on_demand" in environment
        else:
            environment = json.loads(self.compose("config", "--format", "json"))["services"]["worker-download"].get("environment", {})
            enabled = environment.get("DOWNLOAD_WORKER_LIFECYCLE") == "on_demand"
        if not enabled:
            raise ValueError("worker-download must explicitly set DOWNLOAD_WORKER_LIFECYCLE=on_demand")
        self.bridge = BridgeProcess(self.args.docker, backend, self.token, getattr(self.args, "bridge_transport", "cli"))
        value = self.bridge.command("acquire")
        if not value["owned"]:
            raise PermissionError("Another download lifecycle controller owns this project")
        self.owned = True
        # Recover cancelled/expired drain and scan pre-existing work even when
        # the original Pub/Sub notification was lost while this host was down.
        self.bridge.command("cancel")
        container = self.container("worker-download")
        running = bool(container and self.docker("inspect", "--format", "{{.State.Running}}", container) == "true")
        self.state = "awake" if running else "stopped"
        self.last_work = time.monotonic()
        self.emit("connected")

    def run(self):
        def stop(_signum, _frame):
            self.stopping = True
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, stop)
        try:
            while not self.stopping:
                try:
                    if self.bridge is None:
                        self.connect()
                    snapshot = self.bridge.command("snapshot")
                    hint = self.bridge.work_hint
                    self.bridge.work_hint = False
                    self.step(snapshot, hint)
                    self.emit("sample", queue_summary={key:snapshot.get(key) for key in
                        ("runnable", "inflight", "deferred", "next_due_in_s")})
                    self.bridge.wait(2)
                except (PermissionError, ValueError):
                    raise
                except Exception as exc:
                    self.emit("recovery", error_type=type(exc).__name__)
                    if self.owned:
                        # Unknown state must keep downloads resident, never stop.
                        try:
                            self.start()
                        except Exception:
                            self.emit("resident_restore_failed")
                    if self.bridge is not None:
                        self.bridge.close()
                        self.bridge = None
                    end = time.monotonic() + 2
                    while not self.stopping and time.monotonic() < end:
                        time.sleep(.1)
        finally:
            if self.owned:
                if self.bridge:
                    try:
                        self.bridge.command("release")
                    except Exception:
                        pass
                self.start()
                self.emit("resident_restored")
            if self.bridge:
                self.bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--docker", default=shutil.which("docker") or "docker")
    parser.add_argument("--idle-seconds", type=float, default=300)
    parser.add_argument("--start-lead-seconds", type=float, default=10)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--bridge-transport", choices=("cli", "unix"), default="cli",
                        help="Persistent Redis bridge transport; unix falls back to cli for remote Docker endpoints.")
    args = parser.parse_args()
    if args.idle_seconds < 1 or args.start_lead_seconds < 3:
        parser.error("idle-seconds >= 1 and start-lead-seconds >= 3 are required")
    args.compose_file = args.compose_file.resolve(strict=True)
    if args.status_file:
        args.status_file = args.status_file.resolve()
        args.status_file.parent.mkdir(parents=True, exist_ok=True)
    Controller(args).run()


if __name__ == "__main__":
    main()
