# Dataset profile

Source: `data/device-telemetry-dataset.ndjson` (the file provided with the assessment, committed byte-exact; see `.gitattributes`).
This profile was produced by reading the raw NDJSON directly, **independently of the copilot's tools**, and it is the
source of the gold answers used in `evals/golden/` and the detector tests in `tests/test_insights.py`.

## Shape

| Metric | Brief says | Dataset has |
|---|---|---|
| Companies (tenants) | 3 | 3: `acme-001` (10 devices), `globex-002` (8), `initech-003` (7) |
| Devices | 25 | 25 |
| Employees | ~18 | **14** distinct `employee_id`s (5 / 4 / 5); 8 employees have 2-3 devices |
| Duration | 30 days | 2026-05-14 to 2026-06-12 |
| Snapshots | ~750 | 750 = 25 devices x 30 daily snapshots, no duplicates, file grouped by device and chronological |

Each record is one point-in-time snapshot with: `device_id` (= serial number), `company_id`, `employee_id`, `collected_at`,
`agent_version` (always 42.0.0), `os`, `device_identity`, `memory`, `disk_volumes` (always one system volume), `battery`,
`network`, `installed_software`, `compliance_results`. No device changes company, employee, OS version or hardware during the window.

Hardware/OS mix: 17 macOS devices (13.6, 14.5, 15.3, 15.4; arm64) and 8 Windows devices (10 22H2, 11 22H2, 11 23H2; x64).
Models: MacBook Pro, MacBook Air, Mac mini (desktop), ThinkPad X1 Carbon, Dell XPS 15. RAM 8/16/32 GB; system volumes 256 GB to 1 TB.

Compliance catalog (identical on every snapshot): `disk_encryption` (**high**), `screen_lock` (medium), `os_up_to_date` (medium).

## Signals present in the time series

| Pattern | Devices | Evidence in the raw data |
|---|---|---|
| Battery approaching end-of-life | Acme: 7VP16KHV88LM, 8NM23J95R5I6, M4XVHUV1MEPZ | `condition` flips Normal -> "Service Recommended" (05-30, 05-23, 05-24) and stays; `full_charge_capacity` declines linearly by 4.2% over the window; all other batteries have constant capacity and "Normal" |
| RAM consistently constrained | M4XVHUV1MEPZ (30/30 snapshots >= 85%, mean 94.2%), I3JLKILKQTAC (30/30, mean 91.8%), JRZSGXVMKE6M (23/30, mean 87.6%) | `used_memory_bytes / total_memory_bytes` |
| RAM intermittently constrained | CUXW0BQSV1B7 (13/30 >= 85%, mean 85.0%) | borderline: reported as "intermittent" (watch), not "consistent" |
| Storage depleting / consistently low | 8NM23J95R5I6, MT7PJB7N5LRE, E4V0SB5I0MYU, JRZSGXVMKE6M, DARFCP0BQM2G, RLD7WV7J82SH | free space falls ~linearly to 2.0-2.6% free by 06-12; all other devices stay >= 22% free with noise |
| Compliance drift | screen_lock pass -> fail on 10 devices (Acme 5, Globex 2, Initech 3), first failures between 05-22 and 06-05, none recover | fleet-wide screen_lock failures rise from 0/25 to 10/25 |
| Chronic non-compliance | os_up_to_date fails on all 30 snapshots of 19 devices | passes only on macOS 15.4 and Windows 11 23H2 devices |
| High-severity failures | **none** | `disk_encryption` passes on all 750 snapshots (and `encrypted: true` everywhere). "Laptops failing high-severity checks" must be answered "none". |
| New software mid-window | uTorrent (1LYSSFD074BB, Q1AFVLH3YV33, I3JLKILKQTAC, W2656QDAE2ZJ), TeamViewer (DARFCP0BQM2G), CleanMyMac X (7VP16KHV88LM) | appear on a given day and remain; fleet baseline = Chrome, Slack, Zoom, 1Password, VS Code on every device |

## Data-quality quirks (handled explicitly)

| Quirk | Where | Handling |
|---|---|---|
| `battery` section absent | 90 snapshots on the 3 Mac minis (desktops: expected) + 10 sporadic gaps on laptops (9MXJVZ1I0DKA x2, MT7PJB7N5LRE x5, E4V0SB5I0MYU x2, DARFCP0BQM2G x1) | desktops are classified from the model name; for laptops the last available battery reading is used and its date is stated; gaps are surfaced by `get_data_quality_report` |
| `network` section absent | 16 snapshots | not used for any conclusion; reported as a telemetry gap |
| `cycle_count` decreases between consecutive snapshots | every battery device, 7-12 times | treated as approximate; never used as a decision criterion, only as context |
| Mixed OS version formats | "14.5" vs "10 22H2" | platform-aware parsing; macOS and Windows versions are never compared with each other |

## Latest-state answers per tenant (gold)

- Low disk (< 15% free on latest snapshot): Acme 8NM23J95R5I6 (2.6%), MT7PJB7N5LRE (2.0%); Globex E4V0SB5I0MYU (2.4%), JRZSGXVMKE6M (2.0%); Initech DARFCP0BQM2G (2.0%), RLD7WV7J82SH (2.0%).
- macOS older than 15: Acme 4 (1LYSSFD074BB, 7VP16KHV88LM, MT7PJB7N5LRE, XSU9UUDGU6AC); Globex 2 (4SEFDLPTKXAM, QPZSXKINA7GP); Initech 4 (DARFCP0BQM2G, RLD7WV7J82SH, SPKRRDF0L786, XZYK11QTPWEC). 10 fleet-wide.
- Laptops failing high-severity checks: none in any tenant.
