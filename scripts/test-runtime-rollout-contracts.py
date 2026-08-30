#!/usr/bin/env python3
"""Behavioral contracts for Compose/runtime alignment and rollout recovery."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


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


class DeploymentOrderingContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
