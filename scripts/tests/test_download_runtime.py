"""State transitions for host lifecycle control; no Docker/DB side effects."""
import argparse
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
