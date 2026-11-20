"""Detector outputs vs. gold sets derived independently from the raw NDJSON."""

from fleet_copilot.analytics.insights import (
    detect_battery_health,
    detect_compliance_drift,
    detect_memory_pressure,
    detect_software_changes,
    detect_storage_pressure,
    detect_telemetry_gaps,
)
from fleet_copilot.config import Thresholds

TH = Thresholds()


def devices(insights, **filters):
    return sorted(i.device_id for i in insights if i.device_id and all(i.metrics.get(k) == v for k, v in filters.items()))


def test_battery_end_of_life(store):
    acme = detect_battery_health(store.tenant("acme-001"), TH)
    assert devices(acme) == ["7VP16KHV88LM", "8NM23J95R5I6", "M4XVHUV1MEPZ"]
    for i in acme:
        assert i.severity == "high" and i.metrics["condition_latest"] == "Service Recommended"
        assert i.metrics["fade_pct"] == 4.2
        assert len(i.evidence) == 3 and i.explanation
    assert detect_battery_health(store.tenant("globex-002"), TH) == []
    assert detect_battery_health(store.tenant("initech-003"), TH) == []


def test_memory_pressure_sustained_vs_intermittent(store):
    assert devices(detect_memory_pressure(store.tenant("acme-001"), TH), pattern="sustained") == ["M4XVHUV1MEPZ"]
    globex = detect_memory_pressure(store.tenant("globex-002"), TH)
    assert devices(globex, pattern="sustained") == ["JRZSGXVMKE6M"]
    assert devices(globex, pattern="intermittent") == ["CUXW0BQSV1B7"]  # 13/30 snapshots >= 85%
    assert devices(detect_memory_pressure(store.tenant("initech-003"), TH), pattern="sustained") == ["I3JLKILKQTAC"]


def test_storage_pressure(store):
    expected = {"acme-001": ["8NM23J95R5I6", "MT7PJB7N5LRE"], "globex-002": ["E4V0SB5I0MYU", "JRZSGXVMKE6M"],
                "initech-003": ["DARFCP0BQM2G", "RLD7WV7J82SH"]}
    for tenant, devs in expected.items():
        found = detect_storage_pressure(store.tenant(tenant), TH)
        assert devices(found) == devs
        assert all(i.severity == "critical" and i.metrics["slope_gb_per_day"] < 0 for i in found)


def test_compliance_drift(store):
    acme = detect_compliance_drift(store.tenant("acme-001"), TH)
    regressed = sorted(i.device_id for i in acme if i.device_id and i.metrics.get("check_id") == "screen_lock")
    assert regressed == ["1LYSSFD074BB", "8NM23J95R5I6", "DLT35MHGTH2D", "DSYPSES7SKBB", "MT7PJB7N5LRE"]
    fleet = [i for i in acme if i.title == "Fleet compliance drift: screen_lock"][0]
    assert fleet.metrics["failing_first_day"] == 0 and fleet.metrics["failing_last_day"] == 5
    chronic = [i for i in acme if i.title == "Chronic non-compliance: os_up_to_date"][0]
    assert len(chronic.devices) == 9
    # disk_encryption (the only high-severity check) never fails anywhere
    for t in store.company_ids():
        assert not [i for i in detect_compliance_drift(store.tenant(t), TH) if i.metrics.get("check_id") == "disk_encryption"]


def test_software_changes(store):
    initech = detect_software_changes(store.tenant("initech-003"), TH)
    assert sorted((i.device_id, i.metrics["software"]) for i in initech) == [
        ("DARFCP0BQM2G", "TeamViewer"), ("I3JLKILKQTAC", "uTorrent"), ("W2656QDAE2ZJ", "uTorrent")]
    assert all(i.severity == "high" for i in initech)  # both on the review list
    acme = {i.device_id: i for i in detect_software_changes(store.tenant("acme-001"), TH)}
    assert acme["7VP16KHV88LM"].severity == "medium"  # CleanMyMac X: new, but not on the review list
    assert acme["1LYSSFD074BB"].metrics["first_seen"] == "2026-05-31"


def test_telemetry_gaps_are_reported_not_hidden(store):
    gaps = {i.device_id: i for i in detect_telemetry_gaps(store.tenant("acme-001"), TH)}
    assert "battery section missing in 5/30 snapshots" in gaps["MT7PJB7N5LRE"].finding
    assert "DLT35MHGTH2D" not in gaps or "battery" not in gaps["DLT35MHGTH2D"].finding  # desktops have no battery by design
