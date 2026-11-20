from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from privela import data
from privela.config import SiteConfig, load_site_config

PLT_HEADER = ("Geolife trajectory\nWGS 84\nAltitude is in Feet\nReserved 3\n"
              "0,2,255,My Track,0,0,2,8421376\n0\n")
# user -> (start lat, start lon, lat step, lon step) of a generated trajectory in GeoLife format;
# 009 runs close to 000 so that the proximity functions return non-trivial results.
FIXTURE_USERS = {
    "000": (39.9847, 116.3184, -0.000002, -0.00010),
    "009": (39.9856, 116.3170, -0.000010, -0.00008),
    "010": (39.9217, 116.4723, -0.000007, 0.00000),
    "011": (39.9757, 116.3308, -0.000007, -0.000005),
}


def write_geolife(raw_dir: Path, users: dict[str, tuple[float, float, float, float]] = FIXTURE_USERS,
                  fixes: int = 120) -> None:
    """Write one generated ``.plt`` trajectory per user in the GeoLife directory layout."""
    for user, (lat, lon, dlat, dlon) in users.items():
        plt = raw_dir / "Geolife Trajectories 1.3" / "Data" / user / "Trajectory" / "20081023025304.plt"
        plt.parent.mkdir(parents=True)
        rows = "".join(
            f"{lat + i * dlat:.6f},{lon + i * dlon:.6f},0,492,39744.12,2008-10-23,02:{i // 60:02d}:{i % 60:02d}\n"
            for i in range(fixes))
        plt.write_text(PLT_HEADER + rows)


def prepared_site(root: Path) -> SiteConfig:
    """Site config with the generated sample users prepared under ``root``."""
    site = replace(load_site_config(), data_dir=root / "data", processed_dir=root / "data" / "processed")
    write_geolife(site.data_dir / "raw")
    data.prepare_sample(site)
    return site


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SiteConfig:
    """Prepared site whose data and MP-SPDZ input files live in a temp directory."""
    monkeypatch.setattr(data, "REPO_ROOT", tmp_path)
    return prepared_site(tmp_path)
