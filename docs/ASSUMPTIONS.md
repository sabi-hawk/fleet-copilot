# Assumptions and interpretation decisions

The brief leaves these points open. Each decision below is implemented in code, configurable where it is a threshold,
and stated in answers where it matters (for example, answers say which threshold "low disk" means).

## Data semantics

| Topic | Decision | Why / where |
|---|---|---|
| "Current" state | The **latest snapshot of each device**. Answers carry its date. The data ends 2026-06-12; it is a historical export, so the copilot never calls it "live" and never uses the wall clock for trend logic. | `TenantView.latest`, `as_of` |
| Battery when the latest snapshot lacks a battery section | Use the most recent snapshot that has one, and say so with its date | `TenantView.last_with_battery` |
| Laptop vs desktop | From the model name (Mac mini/iMac/... = desktop; MacBook/ThinkPad/XPS/... = laptop), falling back to battery presence | `data/models.py:classify_device_type` |
| GB | 1 GB = 1024^3 bytes, matching the dataset's own convention (`total_memory: "32 GB"` = 34359738368 bytes) | `config.GB` |
| `full_charge_capacity` units | Not stated in the data, so values are reported without units | detectors, evidence claims |
| Company names | Derived from `company_id` (`acme-001` -> "Acme"); the dataset has no name field | `company_display_name` |
| Employees | 14 in the data (brief says ~18). The dataset is treated as the source of truth. | `docs/DATASET.md` |
| OS version comparison | Parsed per family; "older than macOS 15" compares macOS devices only and reports how many devices run another OS family and were not compared | `analytics/versions.py` |

## Thresholds (`src/fleet_copilot/config.py: Thresholds`)

| Name | Default | Meaning |
|---|---|---|
| `low_disk_free_pct` | 15% | latest free space below this = "low on disk" |
| `critical_disk_free_pct` | 10% | below this = critical severity; also the projection target |
| `storage_consistency_share` | 80% | share of snapshots below the low threshold = "consistently low" |
| `storage_horizon_days` | 30 | projected to reach critical within N days = flagged even if not low yet |
| `high_memory_used_pct` | 85% | a snapshot at/above this is memory-constrained |
| `memory_sustained_share` | 50% | constrained in at least this share of snapshots = "consistently constrained" (high) |
| `memory_watch_share` | 25% | at least this share = "intermittently constrained" (medium) |
| `battery_fade_alert_pct` | 3% | full-charge capacity drop over the window that indicates wear |
| `software_review_list` | uTorrent (P2P file sharing), TeamViewer (remote access) | **Assumption:** stands in for a customer-defined software policy, which the brief does not provide. Every new non-baseline app is reported regardless; the list only raises severity. |

The thresholds are engineering defaults, not values from the brief. Sensitivity on this data (measured, not assumed):

- **Low disk** is insensitive: the six low devices sit at 2.0-2.6% free and the next device is at 23.8%, so any threshold
  in that gap gives the same answer.
- **"Consistently constrained by RAM"** is sensitive at the edge: thresholds of 85-88% give {M4XVHUV1MEPZ, I3JLKILKQTAC,
  JRZSGXVMKE6M}; at 84% or lower CUXW0BQSV1B7 (mean exactly 85.0%) joins; at 90% JRZSGXVMKE6M drops out. CUXW0BQSV1B7 is
  therefore reported as *intermittent* (13/30 snapshots) and the eval accepts it either way rather than encoding a
  threshold artefact as ground truth.

## Action semantics

| Topic | Decision |
|---|---|
| Action tool signatures | Exactly the four from the brief: `create_upgrade_order(device_id, component, spec)`, `open_remediation_ticket(device_id, check_id, note)`, `flag_device_for_replacement(device_id, reason)`, `notify_employee(employee_id, message)` |
| What "execute" means | Writing an idempotent, tenant-partitioned record (ticket / order / flag / queued notification) to a JSON outbox (`var/actions/actions.json`) standing in for ITSM, procurement and messaging integrations. No external system is contacted. |
| `component` values | `memory` (alias `ram`), `storage` (aliases `disk`, `ssd`, `drive`), `battery` |
| Replacement-grade evidence | Battery end-of-life (condition not "Normal" or capacity fade >= 3%) or sustained RAM pressure. Storage pressure alone is remediable (cleanup/upgrade), so it is not replacement-grade. |
| Notification evidence | The employee must own a device with an active telemetry-backed issue (failing check, battery wear, sustained RAM pressure, storage pressure, or new software) |
| Who approves | A human identity supplied through the approval channel (CLI prompt / `resolve_approval`). Missing or non-human approver identities ("assistant", "agent", "system", ...) count as rejection. |
| Tenant binding | A session is bound to one tenant at creation. In production this would come from the administrator's authenticated identity; here it is the `--tenant` CLI flag. There is no cross-tenant "platform admin" role, because the brief asks to prevent cross-company access. |

## Out of scope (not invented)

- No real ITSM/procurement/email integrations, authentication or RBAC beyond tenant binding and approver identity.
- No design-capacity battery health percentage (the data has no design capacity field).
- No hardware-upgradeability knowledge (for example, soldered RAM): the policy engine reasons only from telemetry evidence.
