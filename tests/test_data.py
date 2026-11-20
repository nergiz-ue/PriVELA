from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from privela import data

from .conftest import FIXTURE_USERS


def test_prepare_geolife_layout(site) -> None:
    lat, lon, dlat, dlon = FIXTURE_USERS["009"]
    track = data.load_track(site, "009")
    assert len(track.latlon) == data.MAX_TRACK_POINTS
    assert track.latlon[1] == pytest.approx((lat + data.PLT_STRIDE * dlat, lon + data.PLT_STRIDE * dlon))
    assert track.utm_mm[0] == tuple(round(v * data.UTM_SCALE) for v in data.to_utm(lat, lon))
    assert track.h3[0] == data.to_h3(lat, lon)
    assert sorted(p.stem for p in (site.processed_dir / "tracks").glob("*.csv")) == sorted(
        [*FIXTURE_USERS, "000b"])


def test_sample_alias_is_a_later_excerpt_of_user_000(site) -> None:
    alias, user, offset, count = data.SAMPLE_ALIAS
    first = data.load_track(site, user)
    second = data.load_track(site, alias)
    assert len(second.latlon) == count
    assert second.latlon[0] == first.latlon[offset // data.PLT_STRIDE]


def test_prepare_csv_layout(site, tmp_path: Path) -> None:
    raw = tmp_path / "csv"
    raw.mkdir()
    (raw / "042.csv").write_text("lat,lon\n" + "".join(f"39.98,{116.31 + i * 1e-4:.4f}\n" for i in range(8)))
    data.prepare(site, raw_dir=raw)
    assert len(data.load_track(site, "042").latlon) == 8


def test_prepare_rejects_missing_inputs(site, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        data.prepare(site, raw_dir=tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        data.prepare(site, users=["042"])
    with pytest.raises(ValueError):
        data.prepare(site, reps=["utm", "polar"])


def test_unprepared_user_is_reported(site, tmp_path: Path) -> None:
    empty = replace(site, processed_dir=tmp_path / "empty")
    with pytest.raises(FileNotFoundError, match="prepare-data"):
        data.load_track(empty, "000")


def test_road_graph_bundled_and_prepared(site) -> None:
    bundled = data.load_road_graph(site)
    assert bundled["n_nodes"] <= bundled["max_nodes"]
    assert len(bundled["waypoints"]) == len(bundled["zone"]) == 5
    out = site.processed_dir / "road" / "subgraph.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({**bundled, "track_a": [0, 1, 2], "node_b": 2}))
    prepared = data.load_road_graph(site)
    assert prepared["track_a"] == [0, 1, 2]
    assert prepared["node_b"] == 2
