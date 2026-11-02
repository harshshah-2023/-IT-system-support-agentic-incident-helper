import pytest

from incident_helper.ops import OpsError, RollbackRefused


def test_status_reflects_running_version_and_dependencies(store):
    status = store.service_status("checkout-web")
    assert status.state == "degraded"
    assert "payments-api" in status.degraded_dependencies


def test_unknown_service_lists_alternatives(store):
    with pytest.raises(OpsError, match="payments-api"):
        store.service_status("paymnts")


def test_recent_deployments_newest_first(store):
    versions = [d.version for d in store.recent_deployments("payments-api")]
    assert versions == ["2.14.0", "2.13.4", "2.13.3"]


def test_rollback_changes_observable_health_and_writes_audit(store):
    assert store.service_status("payments-api").p95_ms == 4210
    result = store.rollback("payments-api", "2.13.4", approved_by="asha.menon", incident_id="INC-1042")
    after = store.service_status("payments-api")
    assert (after.version, after.state, after.p95_ms) == ("2.13.4", "healthy", 212)
    assert result["from"] == "2.14.0"
    assert store.audit_log()[0]["action"] == "rollback_executed"
    assert store.recent_deployments("payments-api")[0].status == "rollback"


@pytest.mark.parametrize(
    ("service", "version", "reason"),
    [
        ("payments-api", "2.14.0", "already runs"),
        ("payments-api", "0.0.1", "not a known good"),
        ("nope", "1", "Unknown"),
    ],
)
def test_rollback_policy_refuses_and_changes_nothing(store, service, version, reason):
    with pytest.raises(RollbackRefused, match=reason):
        store.rollback(service, version, approved_by="asha.menon")
    assert store.service_status("payments-api").version == "2.14.0"
    assert store.audit_log() == []


def test_reset_restores_fixture_state(store):
    store.rollback("payments-api", "2.13.4", approved_by="asha.menon")
    store.reset()
    assert store.service_status("payments-api").version == "2.14.0"
