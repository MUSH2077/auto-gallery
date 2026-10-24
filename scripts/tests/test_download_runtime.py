"""State transitions for host lifecycle control; no Docker/DB side effects."""
import argparse
import os
import importlib.util
import socket
import tempfile
import threading
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("download_runtime_controller", Path(__file__).parents[1] / "download-runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def state(**values):
    return {"runnable":0, "inflight":0, "deferred":0, "next_due_in_s":None, "supervisor":{}, **values}


class LifecycleTests(unittest.TestCase):
    def controller(self):
        c = module.Controller(argparse.Namespace(idle_seconds=1, start_lead_seconds=10, status_file=None))
        c.emit = Mock()
        c.bridge = Mock()
        c.container = Mock(return_value="download-container")
        c.docker = Mock(return_value="true")
        c.health_checked_at = time.monotonic()
        c.last_work = time.monotonic() - 2
        return c

    def test_delayed_retry_wakes_before_deadline_and_deferred_stays_resident(self):
        self.assertFalse(module.demand(state(next_due_in_s=60), 10))
        self.assertTrue(module.demand(state(next_due_in_s=9), 10))
        self.assertTrue(module.demand(state(deferred=1), 10))
        self.assertTrue(module.demand(state(inflight=1), 10))

    def test_empty_queues_request_drain_without_stopping_container(self):
        c = self.controller()
        c.step(state())
        self.assertEqual(c.state, "draining")
        self.assertTrue(c.drain_token.startswith(c.token + ":"))
        c.docker.assert_not_called()

    def test_stale_ack_never_authorizes_stop(self):
        c = self.controller();c.state = "draining";c.drain_token = "new"
        c.bridge.command.return_value = state()
        c.bridge.command.return_value["supervisor"] = dict(drain_token="old", drain_ack=True)
        c.step(state())
        c.docker.assert_not_called()
        self.assertEqual(c.state, "draining")

    def test_ack_only_stops_then_work_published_during_stop_starts_again(self):
        c = self.controller();c.state = "draining";c.drain_token = "current"
        snapshot = state();snapshot["supervisor"] = dict(drain_token="current", drain_ack=True)
        c.bridge.command.return_value = snapshot
        c.step(state())
        self.assertEqual(c.state, "stopped")
        c.docker.assert_called_once_with("stop", "--time", "5", "download-container")
        c.step(state(runnable=1))
        self.assertEqual(c.state, "awake")
        self.assertEqual(c.docker.call_args.args, ("start", "download-container"))

    def test_racing_runnable_work_cancels_drain(self):
        c = self.controller();c.state = "draining";c.drain_token = "current"
        c.bridge.command.return_value = state(runnable=1)
        c.step(state())
        self.assertEqual(c.state, "awake")
        c.docker.assert_not_called()
        self.assertIsNone(c.drain_token)

    def test_duplicate_hints_do_not_start_a_second_container(self):
        c = self.controller();c.state = "stopped"
        c.step(state(runnable=2), hint=True)
        c.step(state(runnable=2), hint=True)
        self.assertEqual(c.docker.call_count, 1)

    def test_unexpected_container_stop_recovers_even_with_queued_work(self):
        c = self.controller();c.health_checked_at = 0
        c.docker.return_value = "false"
        c.step(state(runnable=1))
        self.assertIn((("start", "download-container"),), [(x.args,) for x in c.docker.call_args_list])
        self.assertEqual(c.state, "awake")

    def test_already_received_work_hint_does_not_sleep_again(self):
        bridge = object.__new__(module.BridgeProcess)
        bridge.work_hint = True
        bridge.receive = Mock()
        bridge.wait(2)
        bridge.receive.assert_not_called()

    def test_native_wsl_recreation_preserves_original_config(self):
        source=chr(92)*2+'wsl$'+chr(92)+'Ubuntu-22.04'+chr(92)+'home'+chr(92)+'user'+chr(92)+'data'
        config={'services':{'worker-download':{'volumes':[{'type':'bind','source':source,'target':'/downloads'}]}}}
        result,changed=module.native_wsl_binds(config,'Ubuntu-22.04')
        self.assertTrue(changed)
        self.assertEqual(result['services']['worker-download']['volumes'][0]['source'],'/home/user/data')
        self.assertEqual(config['services']['worker-download']['volumes'][0]['source'],source)

    def test_host_memory_measurement_includes_controller(self):
        result = module.process_memory()
        self.assertGreater(result["host_pss_bytes"], 0)
        self.assertTrue(result["children_metrics"])



class FakeSocket:
    def __init__(self, received=b""):
        self.received = list(received) if isinstance(received, list) else [received]
        self.sent = []
        self.shutdown_calls = []
        self.timeout = None
        self.closed = False

    def sendall(self, value):
        self.sent.append(value)

    def shutdown(self, how):
        self.shutdown_calls.append(how)

    def settimeout(self, value):
        self.timeout = value

    def recv(self, _size):
        return self.received.pop(0) if self.received else b""

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    @staticmethod
    def bridge_with_buffer():
        bridge = object.__new__(module.UnixDockerBridge)
        bridge.buffer = b""
        bridge.stdout_buffer = b""
        bridge.messages = []
        bridge.work_hint = False
        return bridge

    def test_fragmented_multiplex_frames_and_json_lines(self):
        bridge = self.bridge_with_buffer()
        payload = b'{"id":7,"value":{"owned":true}}\n'
        frame = b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
        bridge.buffer = frame[:5]
        bridge._parse_frames()
        self.assertEqual(bridge.messages, [])
        bridge.buffer += frame[5:]
        bridge._parse_frames()
        self.assertEqual(bridge.messages[0]["id"], 7)

    def test_stderr_is_drained_and_old_response_id_is_preserved(self):
        bridge = self.bridge_with_buffer()
        stderr = b"warning\n"
        stdout = b'{"id":2,"value":"old"}\n'
        bridge.buffer = (b"\x02\x00\x00\x00" + len(stderr).to_bytes(4, "big") + stderr +
                        b"\x01\x00\x00\x00" + len(stdout).to_bytes(4, "big") + stdout)
        bridge._parse_frames()
        self.assertEqual([item["id"] for item in bridge.messages], [2])
        self.assertEqual(bridge.stdout_buffer, b"")

    def test_stdin_uses_raw_json_protocol(self):
        bridge = self.bridge_with_buffer()
        bridge.sock = FakeSocket()
        bridge.send(b'{"id":1}\n')
        self.assertEqual(bridge.sock.sent, [b'{"id":1}\n'])

    def test_close_shuts_down_and_closes_stream(self):
        bridge = self.bridge_with_buffer()
        bridge.sock = FakeSocket([b""])
        bridge.close()
        self.assertEqual(bridge.sock.shutdown_calls, [module.socket.SHUT_WR])
        self.assertTrue(bridge.sock.closed)

    def test_http_error_is_reported_without_logging_body(self):
        bridge = self.bridge_with_buffer()
        bridge.socket_path = "/tmp/fake-docker.sock"
        response = Mock(status=500)
        response.read.return_value = b"private-body"
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(module, "_UnixHTTPConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                bridge._json_request("GET", "/version")

    def test_http_eof_is_reported(self):
        bridge = self.bridge_with_buffer()
        bridge.sock = FakeSocket([b""])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            bridge._read_http()

    def test_cli_fallback_for_remote_endpoint_is_explicit(self):
        with patch.object(module, "UnixDockerBridge", side_effect=module.UnsupportedDockerEndpoint("remote")):
            with patch.object(module.subprocess, "Popen") as popen:
                bridge = module.BridgeProcess("docker", "backend", "token", "unix")
        self.assertEqual(bridge.transport, "cli")
        popen.assert_called_once()

    def test_attach_failure_closes_stream_socket(self):
        fake = FakeSocket()
        with patch.dict(os.environ, {"DOCKER_HOST": "unix:///tmp/fake.sock"}, clear=True):
            with patch.object(module.UnixDockerBridge, "_json_request",
                              side_effect=[{"ApiVersion": "1.43"}, {"Id": "exec-1"}]):
                with patch.object(module.UnixDockerBridge, "_connect", return_value=fake):
                    with patch.object(module.UnixDockerBridge, "_start_stream",
                                      side_effect=RuntimeError("upgrade failed")):
                        with self.assertRaisesRegex(RuntimeError, "upgrade failed"):
                            module.UnixDockerBridge("docker", "backend", "token")
        self.assertTrue(fake.closed)

    def test_endpoint_selection_rejects_remote_docker_host(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://127.0.0.1:2375"}, clear=False):
            self.assertIsNone(module._context_docker_host("docker"))

    def test_complete_unix_engine_handshake_with_chunked_version_and_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = os.path.join(directory, "docker.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(3)
            requests = []
            stdin = []
            failures = []

            def read_request(connection):
                data = b""
                while b"\r\n\r\n" not in data:
                    part = connection.recv(4096)
                    if not part:
                        return data
                    data += part
                header, _, body = data.partition(b"\r\n\r\n")
                lines = header.decode().split("\r\n")
                length = next((int(line.split(":", 1)[1].strip()) for line in lines
                               if line.lower().startswith("content-length:")), 0)
                while len(body) < length:
                    body += connection.recv(4096)
                return header, body[:length]

            def frame(payload):
                return b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload

            def daemon():
                try:
                    for index in range(3):
                        connection, _ = server.accept()
                        request = read_request(connection)
                        requests.append(request[0].splitlines()[0])
                        if index == 0:
                            payload = b'{"ApiVersion":"1.43"}'
                            chunks = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
                            connection.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks)
                            connection.close()
                        elif index == 1:
                            payload = b'{"Id":"exec-1"}'
                            connection.sendall(b"HTTP/1.1 201 Created\r\nContent-Length: " +
                                               str(len(payload)).encode() + b"\r\n\r\n" + payload)
                            connection.close()
                        else:
                            payload = b'{"id":1,"value":{"owned":true}}\n'
                            connection.sendall(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n" + frame(payload))
                            stdin.append(connection.recv(4096))
                            connection.close()
                except Exception as error:
                    failures.append(error)

            thread = threading.Thread(target=daemon, daemon=True)
            thread.start()
            with patch.dict(os.environ, {"DOCKER_HOST": "unix://" + socket_path}, clear=True):
                bridge = module.UnixDockerBridge("docker", "backend", "token")
                self.assertEqual(bridge.receive(0), {"id": 1, "value": {"owned": True}})
                bridge.send(b'{"id":2,"action":"snapshot"}\n')
                bridge.close()
            thread.join(2)
            server.close()
            self.assertFalse(failures)
            self.assertEqual(len(requests), 3)
            self.assertIn(b"GET /version HTTP/1.1", requests[0])
            self.assertIn(b"POST /v1.43/containers/backend/exec HTTP/1.1", requests[1])
            self.assertIn(b"POST /v1.43/exec/exec-1/start HTTP/1.1", requests[2])
            self.assertEqual(stdin, [b'{"id":2,"action":"snapshot"}\n'])

    def test_explicit_context_takes_precedence_over_docker_host(self):
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "selected", "DOCKER_HOST": "tcp://127.0.0.1:2375"}, clear=True):
            result = Mock()
            result.stdout = '"unix:///tmp/context.sock"'
            with patch.object(module.subprocess, "run", return_value=result) as run:
                self.assertEqual(module._context_docker_host("docker"), "/tmp/context.sock")
            self.assertIn("selected", run.call_args.args[0])

    def test_unix_work_hint_reaches_controller_wait(self):
        bridge = object.__new__(module.BridgeProcess)
        bridge.stream = Mock()
        bridge.stream.receive.return_value = {"event": "work"}
        bridge.work_hint = False
        self.assertEqual(bridge.receive(0), {"event": "work"})
        self.assertTrue(bridge.work_hint)
        bridge.wait(2)
        bridge.stream.receive.assert_called_once_with(0)

    def test_command_ignores_old_response_id(self):
        bridge = object.__new__(module.BridgeProcess)
        bridge.sequence = 0
        bridge.stream = Mock()
        bridge.transport = "unix"
        bridge.process = None
        bridge.receive = Mock(side_effect=[
            {"id": 0, "value": "old"},
            {"id": 1, "value": "wanted"},
        ])
        self.assertEqual(bridge.command("snapshot"), "wanted")
        bridge.stream.send.assert_called_once()

    def test_command_timeout_is_bounded(self):
        bridge = object.__new__(module.BridgeProcess)
        bridge.sequence = 0
        bridge.stream = Mock()
        bridge.transport = "unix"
        bridge.process = None
        bridge.receive = Mock(return_value=None)
        with patch.object(module.time, "monotonic", side_effect=[0, 8]):
            with self.assertRaises(TimeoutError):
                bridge.command("snapshot")


if __name__ == "__main__":
    unittest.main()
