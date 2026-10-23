#!/usr/bin/env python3
"""Behavioral contracts for Compose/runtime alignment and rollout recovery."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeResourceContractTests(unittest.TestCase):
    def test_runtime_verifier_accepts_every_resolved_compose_profile(self):
        verifier = _load_script("verify-compose-resources.py")
        test_root = "/tmp/auto-gallery-compose-test/runtime-contract"
        configurations = (
            verifier.compose_config("docker-compose.yaml"),
            verifier.compose_config(
                "docker-compose.yaml",
                "docker-compose.nas-io.yaml",
                extra_env={"NAS_BLOCK_DEVICE": "/dev/sdb"},
            ),
            verifier.compose_config(
                "docker-compose.yaml",
                "docker-compose.test.yaml",
                profiles=("load",),
                extra_env={
                    "TEST_RUN_ID": "runtime-contract",
                    "TEST_ROOT": test_root,
                    "TEST_FIXTURES": f"{test_root}/fixtures",
                    "HOST_POSTGRES": f"{test_root}/postgres",
                    "HOST_REDIS": f"{test_root}/redis",
                    "HOST_MEILISEARCH": f"{test_root}/meilisearch",
                    "HOST_DOWNLOADS": f"{test_root}/downloads",
                    "HOST_LIBRARY": f"{test_root}/library",
                    "HOST_CONFIG_GALLERYDL": f"{test_root}/gallery-dl",
                    "HOST_CONFIG_APP": f"{test_root}/app-config",
                    "HOST_RESTORE_STAGING": f"{test_root}/restore-staging",
                    "HOST_RESTORE_RECEIPTS": f"{test_root}/restore-receipts",
                    "BACKEND_IMAGE": "auto-gallery-backend:candidate-runtime-contract",
                    "ADMIN_IMAGE": "auto-gallery-admin-web:candidate-runtime-contract",
                    "BACKEND_PORT": "18818",
                    "ADMIN_WEB_PORT": "13080",
                },
            ),
        )

        for config in configurations:
            with self.subTest(services=sorted(config["services"])):
                verifier.verify_runtime_resource_contract(config)


class RecoveryGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load_script("verify-resource-recovery.py")

    @staticmethod
    def _health(**pressure_overrides):
        pressure = {
            "status": "warning",
            "controller_mode": "constrained",
            "hard_reasons": [],
            "trigger_reasons": [],
            "recovery_remaining_seconds": 0.0,
            "controller": {
                "governance_mode": "enforce",
                "hard_gate_active": False,
            },
        }
        pressure.update(pressure_overrides)
        return {"resource_pressure": pressure}

    def test_soft_pressure_is_safe_after_controller_recovery(self):
        ready, summary = self.gate.evaluate_recovery(self._health())

        self.assertTrue(ready, summary)

    def test_every_hard_recovery_signal_blocks_worker_release(self):
        blocked = (
            {"status": "paused"},
            {"controller_mode": "critical"},
            {"hard_reasons": ["memory_available_critical"]},
            {"trigger_reasons": ["memory_available_critical"]},
            {"recovery_remaining_seconds": 1.0},
            {"recovery_remaining_seconds": -1.0},
            {
                "controller": {
                    "governance_mode": "enforce",
                    "hard_gate_active": True,
                }
            },
            {
                "controller": {
                    "governance_mode": "shadow",
                    "hard_gate_active": False,
                }
            },
        )

        for override in blocked:
            with self.subTest(override=json.dumps(override, sort_keys=True)):
                ready, _ = self.gate.evaluate_recovery(self._health(**override))
                self.assertFalse(ready)

    def test_recovery_duration_must_be_present_finite_numeric_zero(self):
        invalid_values = (None, True, False, float("nan"), float("inf"), float("-inf"))

        missing = self._health()
        del missing["resource_pressure"]["recovery_remaining_seconds"]
        ready, _ = self.gate.evaluate_recovery(missing)
        self.assertFalse(ready)

        for value in invalid_values:
            with self.subTest(value=repr(value)):
                ready, _ = self.gate.evaluate_recovery(
                    self._health(recovery_remaining_seconds=value)
                )
                self.assertFalse(ready)

    def test_hanging_recovery_probe_is_killed_by_outer_deadline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_docker = Path(temp_dir) / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\nsleep 30\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{temp_dir}:{os.environ['PATH']}",
                "COMPOSE_ENV_FILE": str(Path(temp_dir) / "absent.env"),
                "RECOVERY_CURL_CONNECT_TIMEOUT_SECONDS": "1",
                "RECOVERY_CURL_MAX_TIME_SECONDS": "1",
            }

            started = time.monotonic()
            completed = subprocess.run(
                [
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=1s",
                    "2s",
                    "bash",
                    str(ROOT / "scripts" / "probe-resource-recovery.sh"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )

        self.assertEqual(completed.returncode, 124, completed.stderr)
        self.assertLess(time.monotonic() - started, 5)


class RqWorkerRegistrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = _load_script("verify-rq-worker-registrations.py")

    @staticmethod
    def _payload(*, stale_queue=None, omit_queue=None, omit_role=None):
        fresh = "2026-08-30T10:00:00+00:00"
        stale = "2026-08-30T09:50:00+00:00"
        role_queues = (
            (
                "download",
                [
                    "downloads",
                    "downloads:pixiv",
                    "downloads:danbooru",
                    "downloads:iwara",
                    "downloads:weibo",
                    "downloads:bilibili",
                    "downloads:pinterest",
                    "downloads:lofter",
                    "downloads:x",
                ],
            ),
            ("import", ["imports"]),
            ("import", ["maintenance"]),
            ("operations", ["operations"]),
            ("discovery", ["discovery"]),
            ("scheduler", ["scheduled"]),
        )
        workers = []
        for role, registered in role_queues:
            if role == omit_role:
                continue
            remaining = [name for name in registered if name != omit_queue]
            if not remaining:
                continue
            workers.append(
                {
                    "hostname": f"current-{role}",
                    "queues": remaining,
                    "last_heartbeat": stale if stale_queue in registered else fresh,
                }
            )
        return {
            "observed_at": "2026-08-30T10:05:00+00:00",
            "expected_hostnames": {
                role: f"current-{role}"
                for role in ("download", "import", "operations", "discovery", "scheduler")
                if role != omit_role
            },
            "workers": workers,
        }

    def test_missing_core_role_hostname_fails_closed(self):
        payload = self._payload()
        del payload["expected_hostnames"]["operations"]
        ready, summary = self.verifier.evaluate_registrations(payload)
        self.assertFalse(ready)
        self.assertIn("operations", summary)

    def test_disabled_topology_without_discovery_role_passes(self):
        ready, summary = self.verifier.evaluate_registrations(
            self._payload(omit_role="discovery")
        )
        self.assertTrue(ready, summary)

    def test_enabled_topology_missing_discovery_coverage_fails(self):
        ready, summary = self.verifier.evaluate_registrations(
            self._payload(omit_queue="discovery")
        )
        self.assertFalse(ready)
        self.assertIn("discovery", summary)

    def test_actual_service_queue_topology_accepts_separate_child_registrations(self):
        ready, summary = self.verifier.evaluate_registrations(self._payload())

        self.assertTrue(ready, summary)

    def test_missing_owned_queue_fails_closed(self):
        ready, summary = self.verifier.evaluate_registrations(
            self._payload(omit_queue="maintenance")
        )

        self.assertFalse(ready)
        self.assertIn("maintenance", summary)

    def test_stale_registration_does_not_count_as_queue_coverage(self):
        ready, summary = self.verifier.evaluate_registrations(
            self._payload(stale_queue="scheduled")
        )

        self.assertFalse(ready)
        self.assertIn("scheduled", summary)

    def test_fresh_old_container_registrations_cannot_cover_current_container(self):
        payload = self._payload(omit_queue="maintenance")
        old_workers = self._payload()["workers"]
        for worker in old_workers:
            worker["hostname"] = f"old-{worker['hostname']}"
        payload["workers"].extend(old_workers)

        ready, summary = self.verifier.evaluate_registrations(payload)

        self.assertFalse(ready)
        self.assertIn("maintenance", summary)

    def test_full_runtime_verification_queries_rq_and_runs_coverage_check(self):
        runtime = (ROOT / "scripts" / "verify-runtime.sh").read_text(
            encoding="utf-8"
        )
        full_check = runtime.split('if [[ "$verify_scope" == "full" ]]', 1)[1]

        self.assertIn("Worker.all(connection=connection)", full_check)
        self.assertIn("verify-rq-worker-registrations.py", full_check)
        self.assertIn("VERIFY_RQ_HEARTBEAT_MAX_AGE_SECONDS", full_check)
        self.assertIn("current_container_hostname", full_check)
        self.assertIn('"hostname": getattr(worker, "hostname", None)', full_check)
        self.assertIn('"expected_hostnames":', full_check)

    def test_expected_queue_roles_match_resolved_compose_topology(self):
        compose_verifier = _load_script("verify-compose-resources.py")
        config = compose_verifier.compose_config("docker-compose.yaml")
        service_roles = {
            "worker-download": "download",
            "worker-import": "import",
            "worker-operations": "operations",
            "worker-discovery": "discovery",
            "scheduler": "scheduler",
        }

        for service, role in service_roles.items():
            definition = config["services"][service]
            command = " ".join(definition["command"])
            match = re.search(r"worker_entrypoint\.py\s+([^\s]+)", command)
            self.assertIsNotNone(match, service)
            queues = set(match.group(1).split(","))
            extras = definition.get("environment", {}).get("WORKER_EXTRA_QUEUES", "")
            queues.update(name.strip() for name in extras.split(",") if name.strip())

            self.assertEqual(queues, self.verifier.ROLE_QUEUES[role], service)


class RuntimeServiceSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = _load_script("runtime-services.py")

    @staticmethod
    def _config(**environment):
        return {"services": {"backend": {"environment": environment}}}

    def test_disabled_effective_flags_omit_discovery(self):
        services = self.resolver.worker_services(
            self._config(
                REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED="false",
                REMOTE_DISCOVERY_PIXIV_PREVIEW_ENABLED="true",
            )
        )
        self.assertEqual(
            services,
            ["worker-download", "worker-import", "worker-operations", "scheduler"],
        )

    def test_live_container_environment_parser_detects_enabled_producer(self):
        self.assertTrue(
            self.resolver.discovery_enabled_environment(
                [
                    "REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED=true",
                    "REMOTE_DISCOVERY_X_ENABLED=true",
                ]
            )
        )
        self.assertFalse(
            self.resolver.discovery_enabled_environment(
                [
                    "REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED=false",
                    "REMOTE_DISCOVERY_X_ENABLED=true",
                ]
            )
        )

    def test_private_members_and_provider_preview_enable_discovery(self):
        services = self.resolver.worker_services(
            self._config(
                REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED="true",
                REMOTE_DISCOVERY_X_ENABLED="true",
            )
        )
        self.assertIn("worker-discovery", services)

    def test_scheduler_enabled_override_keeps_discovery_with_backend_disabled(self):
        config = self._config(REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED="false")
        config["services"]["scheduler"] = {"environment": {
            "REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED": "true",
            "REMOTE_DISCOVERY_X_ENABLED": "true",
        }}
        self.assertIn("worker-discovery", self.resolver.worker_services(config))

    def test_persisted_nonterminal_discovery_task_blocks_disabled_selection(self):
        self.assertTrue(
            self.resolver.discovery_work_pending(
                {"queue": {}, "persisted_nonterminal": 1}
            )
        )

    def test_deferred_registry_blocks_disabled_selection(self):
        self.assertTrue(
            self.resolver.discovery_work_pending(
                {"queue": {"deferred": 1}, "persisted_nonterminal": 0}
            )
        )

    def test_retry_registry_blocks_disabled_selection(self):
        self.assertTrue(
            self.resolver.discovery_work_pending(
                {"queue": {"retry": 1}, "persisted_nonterminal": 0}
            )
        )

    def test_invalid_backlog_input_fails_closed(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "runtime-services.py"),
                "--check-discovery-backlog",
            ],
            input="{",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_effective_flag_fails_closed(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "runtime-services.py")],
            input=json.dumps(
                self._config(
                    REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED="maybe",
                )
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_deploy_refuses_running_workers_when_backend_is_unavailable(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("compose ps worker-discovery scheduler", deploy)
        self.assertIn(
            "background discovery/scheduler workers are running while backend is unavailable",
            deploy,
        )

    def test_disabled_deploy_refuses_live_enabled_producers_before_mutation(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("live_discovery_producer_enabled", deploy)
        self.assertIn(
            "live backend/scheduler discovery producers are still enabled",
            deploy,
        )
        self.assertLess(
            deploy.index("live_discovery_producer_enabled"),
            deploy.index("compose stop -t 120 worker-download"),
        )

    def test_fresh_deploy_defers_probe_until_backend_exists(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('compose ps backend --format', deploy)
        self.assertLess(
            deploy.index("compose config --quiet\nresolve_background_services"),
            deploy.index("compose stop -t 120 worker-download"),
        )
        self.assertGreater(
            deploy.rindex("check_disabled_discovery_backlog"),
            deploy.index('VERIFY_SCOPE=core'),
        )


class DeploymentOrderingContractTests(unittest.TestCase):
    def test_service_resolution_executes_and_propagates_pending_refusal(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        functions = deploy.split("discovery_queue_has_work() {", 1)[1]
        functions = "discovery_queue_has_work() {" + functions.split("all_services_ready() {", 1)[0]
        stub = """set -euo pipefail
compose() {
  case "$1" in
    config) printf '%s' "$RUNTIME_TEST_CONFIG" ;;
    ps)
      case "$RUNTIME_TEST_PS_FAIL:$2" in producer:-q|backend:backend|background:worker-discovery) return 9 ;; esac
      if [[ "$2" == backend && "$RUNTIME_TEST_BACKEND" == running ]]; then printf 'running'; fi ;;
    exec) cat >/dev/null; printf '%s' "$RUNTIME_TEST_BACKLOG" ;;
    *) return 2 ;;
  esac
}
"""
        script = stub + functions + """
BACKGROUND_SERVICES=()
if resolve_background_services; then
  printf '%s\\n' "${BACKGROUND_SERVICES[@]}"
else
  exit 77
fi
"""
        for name, running, enabled, pending, ps_failure, code in [
            ("fresh", "", False, 0, "", 0),
            ("idle", "running", False, 0, "", 0),
            ("pending", "running", False, 1, "", 77),
            ("enabled", "", True, 0, "", 0),
            ("malformed", "", "invalid", 0, "", 77),
            ("producer-inspection-failed", "", False, 0, "producer", 77),
            ("backend-inspection-failed", "", False, 0, "backend", 77),
            ("background-inspection-failed", "", False, 0, "background", 77),
        ]:
            with self.subTest(name=name):
                environment = {
                    "REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED": enabled,
                    "REMOTE_DISCOVERY_PIXIV_PREVIEW_ENABLED": True,
                }
                result = subprocess.run(
                    ["bash", "-c", script], cwd=ROOT, text=True, capture_output=True,
                    env={**os.environ,
                        "RUNTIME_TEST_CONFIG": json.dumps({"services": {"backend": {"environment": environment}}}),
                        "RUNTIME_TEST_BACKEND": running,
                        "RUNTIME_TEST_PS_FAIL": ps_failure,
                        "RUNTIME_TEST_BACKLOG": json.dumps({"queue": {}, "persisted_nonterminal": pending})},
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                if code == 0:
                    self.assertEqual("worker-discovery" in result.stdout, enabled)
                    self.assertIn("scheduler", result.stdout)

    def test_verification_refuses_unreadable_discovery_service_state(self):
        verify = (ROOT / "scripts" / "verify-runtime.sh").read_text(encoding="utf-8")
        section = verify.split('if [[ "$verify_scope" == "full" ]]; then', 1)[1]
        section = 'if [[ "$verify_scope" == "full" ]]; then' + section.split("require_service_ok() {", 1)[0]
        script = """set -euo pipefail
verify_scope=full
services=()
application_services=()
compose() {
  case "$1" in
    config) printf '{"services":{"backend":{"environment":{}}}}' ;;
    ps) return 9 ;;
  esac
}
""" + section
        result = subprocess.run(["bash", "-c", script], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not inspect discovery service state", result.stderr)

    def test_deploy_waits_for_natural_hard_recovery_before_workers(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        rollout = deploy.split(
            "# ── 7. Project-local verification and background startup", 1
        )[1]

        recovery = rollout.index("wait_for_resource_recovery")
        core_verification = rollout.index("VERIFY_SCOPE=core")
        worker_start = rollout.index("Starting adaptive background workers")
        self.assertLess(recovery, core_verification)
        self.assertLess(core_verification, worker_start)
        self.assertNotIn("VERIFY_ALLOW_CRITICAL_PRESSURE=1", rollout)
        self.assertNotIn("resource:pressure:latch", deploy)

    def test_recovery_poll_has_curl_and_outer_process_deadlines(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe-resource-recovery.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("timeout --signal=TERM", deploy)
        self.assertIn("probe-resource-recovery.sh", deploy)
        self.assertIn("--connect-timeout", probe)
        self.assertIn("--max-time", probe)


class RunbookSafetyContractTests(unittest.TestCase):
    def test_normal_infrastructure_changes_use_safe_deploy(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        section = runbook.split("### Deploying Only Infrastructure Changes", 1)[1]
        section = section.split("\n## ", 1)[0]

        self.assertIn("bash scripts/deploy.sh", section)
        self.assertNotIn("docker compose up -d --force-recreate", section)

    def test_break_glass_stops_writers_and_foreground_before_backup_and_migrate(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        section = runbook.split("### Manual Deploy (break-glass only)", 1)[1]
        section = section.split("### Deploying Only Infrastructure Changes", 1)[0]

        workers_stopped = section.index(
            "docker compose stop -t 120 worker-download worker-import worker-operations worker-discovery scheduler"
        )
        foreground_stopped = section.index(
            "docker compose stop -t 120 admin-web backend"
        )
        backup = section.index("checksummed rollback point")
        migrate = section.index("docker compose up --no-deps migrate")
        recovery_gate = section.index("probe-resource-recovery.sh")
        core_verification = section.index("VERIFY_SCOPE=core")
        worker_start = section.index(
            "docker compose up -d worker-download"
        )

        self.assertLess(workers_stopped, foreground_stopped)
        self.assertLess(foreground_stopped, backup)
        self.assertLess(backup, migrate)
        self.assertLess(migrate, recovery_gate)
        self.assertLess(recovery_gate, core_verification)
        self.assertLess(core_verification, worker_start)
        self.assertLess(recovery_gate, worker_start)

    def test_break_glass_failures_exit_before_worker_start(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        section = runbook.split("### Manual Deploy (break-glass only)", 1)[1]
        section = section.split("### Deploying Only Infrastructure Changes", 1)[0]

        self.assertIn("if ! (", section)
        self.assertIn("if ! VERIFY_SCOPE=core", section)

        recovery_guard = section.index("if ! (")
        recovery_exit = section.index("exit 1", recovery_guard)
        core_guard = section.index("if ! VERIFY_SCOPE=core")
        core_exit = section.index("exit 1", core_guard)
        worker_start = section.index("docker compose up -d worker-download")

        self.assertLess(recovery_guard, recovery_exit)
        self.assertLess(recovery_exit, core_guard)
        self.assertLess(core_guard, core_exit)
        self.assertLess(core_exit, worker_start)


if __name__ == "__main__":
    unittest.main()
