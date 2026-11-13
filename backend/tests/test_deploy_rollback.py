"""Execute application rollback with controlled Docker command results."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("failure", ["stop", "status", "active", "timeout", None])
def test_deploy_rollback_requires_stopped_writers(tmp_path, failure):
    script = Path(__file__).resolve().parents[2] / "scripts/deploy-rollback.sh"
    target = tmp_path / "rollback.sh"
    target.write_bytes(script.read_bytes())
    (tmp_path / "manifest.env").write_text(f"""DEPLOYMENT_ID=fixture
PROJECT_ROOT={tmp_path}
PREDEPLOY_GIT_HEAD=fixture
PREDEPLOY_ALEMBIC_REVISION=aaaaaaaaaaaa
CANDIDATE_ALEMBIC_REVISION=bbbbbbbbbbbb
BACKEND_IMAGE_ID=fixture-backend
BACKEND_ROLLBACK_TAG=fixture-backend-tag
ADMIN_IMAGE_ID=fixture-admin
ADMIN_ROLLBACK_TAG=fixture-admin-tag
ROLLBACK_SCHEMA_POLICY=schema-forward
ROLLBACK_SCHEMA_CURRENT_REVISION_AT_SNAPSHOT=aaaaaaaaaaaa
ROLLBACK_SCHEMA_RETAIN_CANDIDATE=true
ROLLBACK_OLD_MIGRATE_ONLY_AT_PREDEPLOY=true
""")
    log = tmp_path / "commands"
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f: f.write(' '.join(args)+'\\n')
failure = os.environ.get('DOCKER_FAILURE')
if 'stop' in args and failure == 'stop': sys.exit(1)
if 'ps' in args and '--all' in args:
    if failure == 'status': sys.exit(1)
    if failure == 'active': print('fixture-writer')
elif 'exec' in args and 'postgres' in args: print('bbbbbbbbbbbb')
""")
    docker.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text("""#!/usr/bin/env python3
import os, subprocess, sys
if os.environ.get('DOCKER_FAILURE') == 'timeout': sys.exit(124)
sys.exit(subprocess.run(sys.argv[2:]).returncode)
""")
    timeout.chmod(0o700)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "COMMAND_LOG": str(log), "DOCKER_FAILURE": failure or ""}
    result = subprocess.run(["bash", str(target)], env=env, capture_output=True, text=True, timeout=10)
    receipt = (tmp_path / "rollback-receipt.env").read_text()
    commands = log.read_text().splitlines()
    if failure:
        assert result.returncode != 0
        assert "ROLLBACK_STATUS=paused" in receipt
        assert "ROLLBACK_WRITER_STOP_CONFIRMED=false" in receipt
        assert not any("image tag" in command or " up " in command for command in commands)
        retried = subprocess.run(["bash", str(target)], env={**env, "DOCKER_FAILURE": ""}, capture_output=True, text=True, timeout=10)
        assert retried.returncode == 0, retried.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "ROLLBACK_STATUS=complete" in receipt
        assert "ROLLBACK_WRITER_STOP_CONFIRMED=true" in receipt
