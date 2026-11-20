"""Benchmark result schema and JSON/CSV writers.

Results merge by key ``(function, representation, protocol, mode)``
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import REPO_ROOT

RESULTS_DIR = REPO_ROOT / "results"

_KEY_FIELDS = ("function", "representation", "protocol", "mode")
_CSV_FIELDS = [
    "function", "representation", "protocol", "mode", "n", "success",
    "mpc_result", "expected", "verified",
    "mpc_time", "online_time", "offline_time", "communication_mb", "rounds", "error",
]


@dataclass
class Result:
    function: str
    representation: str
    protocol: str
    mode: str                       # "local" | "distributed"
    n: Optional[int] = None
    success: bool = False
    mpc_result: Optional[int] = None
    expected: Optional[int] = None
    verified: Optional[bool] = None
    mpc_time: Optional[float] = None
    online_time: Optional[float] = None
    offline_time: Optional[float] = None
    communication_mb: Optional[float] = None
    rounds: Optional[int] = None
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple:
        return tuple(getattr(self, f) for f in _KEY_FIELDS)


def _out_paths(function_class: str) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return (RESULTS_DIR / f"{function_class}.json", RESULTS_DIR / f"{function_class}.csv")


def save(results: list[Result], function_class: str) -> Path:
    """Merge ``results`` into results/<class>.{json,csv} by key; return the JSON path."""
    json_path, csv_path = _out_paths(function_class)

    merged: dict[tuple, dict] = {}
    if json_path.exists():
        for row in json.loads(json_path.read_text()):
            merged[tuple(row[f] for f in _KEY_FIELDS)] = row
    for r in results:
        merged[r.key()] = asdict(r)

    rows = list(merged.values())
    json_path.write_text(json.dumps(rows, indent=2))
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return json_path
