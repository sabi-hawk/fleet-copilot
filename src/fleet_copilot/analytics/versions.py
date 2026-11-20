"""Platform-aware OS version parsing.

The dataset mixes macOS versions ("14.5", "15.4") with Windows releases
("10 22H2", "11 23H2"). A naive string comparison would rank "10 22H2" above
"15.4", so versions are parsed into comparable tuples per OS family and
comparisons across families are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WIN_RE = re.compile(r"^\s*(\d+)\s*(?:(\d{2})H([12]))?\s*$", re.IGNORECASE)
_NUM_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")


@dataclass(frozen=True)
class OSVersion:
    family: str  # "macOS", "Windows", or the raw product name
    key: tuple[int, int, int]
    raw: str

    @property
    def major(self) -> int:
        return self.key[0]


def normalize_family(name: str | None) -> str:
    n = (name or "").strip().lower()
    if n in ("macos", "mac os", "mac os x", "osx", "darwin"):
        return "macOS"
    if n.startswith("windows") or n in ("win32", "win"):
        return "Windows"
    return (name or "unknown").strip()


def parse_os(product_name: str | None, product_version: str | None) -> OSVersion | None:
    family = normalize_family(product_name)
    raw = (product_version or "").strip()
    if not raw:
        return None
    if family == "Windows":
        m = _WIN_RE.match(raw)
        if not m:
            return None
        major = int(m.group(1))
        release = int(m.group(2)) * 10 + int(m.group(3)) if m.group(2) else 0
        return OSVersion(family, (major, release, 0), raw)
    m = _NUM_RE.match(raw)
    if not m:
        return None
    return OSVersion(family, tuple(int(g) if g else 0 for g in m.groups()), raw)  # type: ignore[arg-type]


def parse_bound(family: str, text: str) -> tuple[int, int, int]:
    """Parse a user-supplied version bound such as '15', '14.5' or '11 23H2'."""
    v = parse_os(family, text)
    if v is None:
        raise ValueError(f"Cannot parse {family} version bound '{text}'")
    return v.key


def compare_to_bound(version: OSVersion | None, family: str, bound: str) -> int | None:
    """-1 if older than bound, 0 if equal, 1 if newer; None if not comparable (other OS family)."""
    fam = normalize_family(family)
    if version is None or version.family != fam:
        return None
    b = parse_bound(fam, bound)
    # Compare only as many components as the bound specifies meaningfully (e.g. '15' => major only).
    if fam != "Windows" and re.fullmatch(r"\s*\d+\s*", bound):
        a, bb = (version.key[0],), (b[0],)
    else:
        a, bb = version.key, b
    return (a > bb) - (a < bb)
