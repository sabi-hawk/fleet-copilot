import pytest

from fleet_copilot.analytics.versions import compare_to_bound, parse_os
from fleet_copilot.data import NotFoundInTenant
from fleet_copilot.data.loader import load_ndjson


def test_dataset_shape(store):
    assert store.snapshot_count == 750
    assert store.load_issues == []
    assert store.company_ids() == ["acme-001", "globex-002", "initech-003"]
    sizes = {c: len(store.tenant(c).device_ids()) for c in store.company_ids()}
    assert sizes == {"acme-001": 10, "globex-002": 8, "initech-003": 7}
    assert sum(len(store.tenant(c).employees()) for c in store.company_ids()) == 14  # brief says ~18; data has 14
    for c in store.company_ids():
        v = store.tenant(c)
        assert all(len(v.history(d)) == 30 for d in v.device_ids())
        assert v.window_label == "2026-05-14 to 2026-06-12"


def test_snapshot_traceability(store):
    v = store.tenant("acme-001")
    s = v.history("1LYSSFD074BB")[0]
    assert s.snapshot_id == "1LYSSFD074BB@2026-05-14T09:54:00Z"
    assert s.source_line == 1  # first line of the NDJSON file


def test_tenant_view_isolation_and_callback(store):
    events = []
    acme = store.tenant("acme-001", on_cross_tenant=lambda k, i, o: events.append((k, i, o)))
    with pytest.raises(NotFoundInTenant) as foreign:
        acme.history("QPZSXKINA7GP")  # a Globex device
    with pytest.raises(NotFoundInTenant) as missing:
        acme.history("ZZZZ99999999")
    # identical message for foreign and non-existent IDs: no existence oracle
    assert str(foreign.value).replace("QPZSXKINA7GP", "X") == str(missing.value).replace("ZZZZ99999999", "X")
    assert events == [("device", "QPZSXKINA7GP", "globex-002")]
    assert not acme.has_employee("emp-globex-1002")


def test_device_type_and_battery_fallback(store):
    v = store.tenant("acme-001")
    assert v.device_type("DLT35MHGTH2D") == "desktop"  # Mac mini, no battery section at all
    assert v.device_type("MT7PJB7N5LRE") == "laptop"
    assert v.latest("MT7PJB7N5LRE").battery is None  # latest snapshot lacks battery
    assert v.last_with_battery("MT7PJB7N5LRE").date == "2026-06-11"


def test_loader_records_bad_lines(tmp_path):
    p = tmp_path / "bad.ndjson"
    p.write_text('{"device_id": "A", "company_id": "c-1", "collected_at": "2026-01-01T00:00:00Z"}\nnot json\n{"company_id": "c-1"}\n',
                 encoding="utf-8")
    res = load_ndjson(p)
    assert len(res.snapshots) == 1
    assert [i.line for i in res.issues] == [2, 3]


@pytest.mark.parametrize("name,ver,family,bound,expected", [
    ("macOS", "14.5", "macOS", "15", -1),
    ("macOS", "15.4", "macOS", "15", 0),
    ("macOS", "13.6", "macOS", "14.5", -1),
    ("macOS", "15.3", "macOS", "15.3", 0),
    ("Windows", "10 22H2", "Windows", "11", -1),
    ("Windows", "11 22H2", "Windows", "11 23H2", -1),
    ("Windows", "11 23H2", "macOS", "15", None),  # not comparable across families
])
def test_os_version_comparison(name, ver, family, bound, expected):
    assert compare_to_bound(parse_os(name, ver), family, bound) == expected
