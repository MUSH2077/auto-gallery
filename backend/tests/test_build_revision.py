from pathlib import Path

import yaml


def test_health_snapshot_exposes_configured_build_revision(monkeypatch):
    from app import main
    from app.schemas.system import HealthResponse

    monkeypatch.setattr(main.settings, "build_revision", "revision-under-test")

    assert main._starting_health_snapshot()["build_revision"] == "revision-under-test"
    assert '"build_revision": settings.build_revision' in Path(
        main.__file__
    ).read_text(encoding="utf-8")
    assert "build_revision" in HealthResponse.model_fields


def test_compose_passes_one_build_revision_to_every_service():
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        assert service.get("environment", {}).get("BUILD_REVISION") == "${BUILD_REVISION:-development}", name


def test_public_source_contract_adds_auth_and_credential_states():
    from app.schemas.repository import RepositoryRead
    from app.schemas.subscription_source import SubscriptionSourceRead

    for schema in (RepositoryRead, SubscriptionSourceRead):
        assert "auth_state" in schema.model_fields
        assert "credential_state" in schema.model_fields


def test_workbench_contract_exposes_auth_classification_counts():
    from app.schemas.task_actions import WorkbenchSummary

    schema = WorkbenchSummary.model_json_schema()
    attention = schema["$defs"]["WorkbenchAttention"]["properties"]

    assert "auth_actionable_count" in attention
    assert "auth_disabled_or_unchecked_count" in attention
    assert "credential_issue_count" in attention
    assert "auth_unhealthy_count" in attention
