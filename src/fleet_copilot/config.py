"""Central configuration: thresholds, model settings and runtime paths.

Every threshold the copilot uses to call something "low", "constrained" or
"end-of-life" lives here so that it is documented, overridable and cited in
answers. None of these values come from the assessment brief; they are
engineering defaults documented in docs/ASSUMPTIONS.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "data" / "device-telemetry-dataset.ndjson"
DEFAULT_HOME = PROJECT_ROOT / "var"

# 1 GB in this project = 1024**3 bytes, matching the dataset's own convention
# (device_identity.total_memory "32 GB" == memory.ram_bytes 34359738368).
GB = 1024**3

# Models that support the server-side refusal fallback parameter.
REFUSAL_FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}


@dataclass(frozen=True)
class Thresholds:
    # Storage
    low_disk_free_pct: float = 15.0          # latest free space below this => "low on disk"
    critical_disk_free_pct: float = 10.0     # below this => critical
    storage_consistency_share: float = 0.8   # share of snapshots below low => "consistently low"
    storage_horizon_days: int = 30           # projected to hit critical within N days => flagged
    # Memory
    high_memory_used_pct: float = 85.0       # a snapshot at/above this is "constrained"
    memory_sustained_share: float = 0.5      # >= this share of snapshots constrained => "consistently constrained"
    memory_watch_share: float = 0.25         # >= this share => "intermittent" (watch list)
    # Battery
    battery_fade_alert_pct: float = 3.0      # full_charge_capacity drop over the window that signals wear
    # Software review list (configurable; see docs/ASSUMPTIONS.md). Matching is case-insensitive substring.
    software_review_list: tuple[tuple[str, str], ...] = (
        ("utorrent", "peer-to-peer file-sharing client"),
        ("teamviewer", "third-party remote-access tool"),
    )


@dataclass(frozen=True)
class Settings:
    dataset_path: Path = DEFAULT_DATASET
    home: Path = DEFAULT_HOME
    model: str = "claude-opus-5-5"
    effort: str = "high"
    refusal_fallback: bool = True
    gemini_model: str = "gemini-3.5-flash"
    gemini_thinking_level: str = "LOW"  # MINIMAL | LOW | MEDIUM | HIGH | none (omit)
    llm_timeout_s: float = 120.0  # per-request timeout for LLM calls
    llm_min_interval_s: float = 0.0  # pacing between LLM calls (useful on free-tier rate limits)
    max_agent_iterations: int = 10
    max_reflections: int = 1
    max_revisions: int = 1
    max_proposals_per_turn: int = 10
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def audit_path(self) -> Path:
        return self.home / "audit" / "audit.jsonl"

    @property
    def actions_path(self) -> Path:
        return self.home / "actions" / "actions.json"

    def with_home(self, home: Path) -> "Settings":
        return replace(self, home=Path(home))

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        return replace(
            base,
            dataset_path=Path(os.environ.get("FLEET_COPILOT_DATASET", base.dataset_path)),
            home=Path(os.environ.get("FLEET_COPILOT_HOME", base.home)),
            model=os.environ.get("FLEET_COPILOT_MODEL", base.model),
            effort=os.environ.get("FLEET_COPILOT_EFFORT", base.effort),
            refusal_fallback=os.environ.get("FLEET_COPILOT_REFUSAL_FALLBACK", "on").lower() not in {"0", "off", "false", "no"},
            gemini_model=os.environ.get("FLEET_COPILOT_GEMINI_MODEL", base.gemini_model),
            gemini_thinking_level=os.environ.get("FLEET_COPILOT_GEMINI_THINKING", base.gemini_thinking_level),
            llm_timeout_s=float(os.environ.get("FLEET_COPILOT_LLM_TIMEOUT", base.llm_timeout_s)),
            llm_min_interval_s=float(os.environ.get("FLEET_COPILOT_LLM_MIN_INTERVAL", base.llm_min_interval_s)),
        )


def llm_credentials_available() -> bool:
    """True when the Anthropic SDK can find credentials in the environment."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def gemini_credentials_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
