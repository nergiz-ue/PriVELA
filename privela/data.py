"""Data preparation and MPC input generation.

Two jobs:
  1. ``prepare`` converts raw GPS tracks (GeoLife ``.plt`` or plain ``lat,lon`` CSV) into the three
     representations and stores them under the processed directory:

         <processed>/tracks/<user>.csv     lat, lon, utm_x_mm, utm_y_mm, h3
         <processed>/road/subgraph.json    road subgraph + map-matched node tracks

  2. ``write_inputs`` writes MP-SPDZ ``Player-Data`` (private) and ``Programs/Public-Input``
     (public) files for a given (function, representation) before a run.

Runs read tracks and the road subgraph from the processed directory. GPS data is not bundled
(GeoLife may not be redistributed); the road kernels fall back to the bundled OpenStreetMap
subgraph (``data/road/subgraph.json``) until a road representation has been prepared.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from .config import REPO_ROOT, RepSpec, SiteConfig

log = logging.getLogger(__name__)

# --- Representation constants ---------------------------------------------------------
UTM_CRS = "EPSG:32650"          # Beijing, UTM zone 50N
UTM_SCALE = 1000                # metres -> integer millimetres
H3_RESOLUTION = 8

BUNDLED_ROAD_GRAPH = REPO_ROOT / "data" / "road" / "subgraph.json"


# --- Converters (raw lat/lon -> representation) ---------------------------------------
_utm_transformer = None


def to_utm(lat: float, lon: float) -> tuple[float, float]:
    """Project WGS84 lat/lon to UTM easting/northing (metres)."""
    global _utm_transformer
    if _utm_transformer is None:
        from pyproj import Transformer
        _utm_transformer = Transformer.from_crs("EPSG:4326", UTM_CRS, always_xy=True)
    easting, northing = _utm_transformer.transform(lon, lat)
    return easting, northing


def to_h3(lat: float, lon: float, resolution: int = H3_RESOLUTION) -> str:
    import h3
    return h3.latlng_to_cell(lat, lon, resolution)


def h3_disk_cells(cell: str, hops: int) -> list[str]:
    import h3
    return list(h3.grid_disk(cell, hops))


def h3_ordered_disk(cell: str, max_radius: int) -> list[int]:
    """Disk cells grouped ring-by-ring (ring 0, ring 1, ...), as integers.

    The order matches the positional ring labels used by the H3 distance kernels.
    """
    import h3
    out: list[int] = []
    for d in range(max_radius + 1):
        out.extend(cell_to_int(c) for c in h3.grid_ring(cell, d))
    return out


def cell_to_int(cell: str) -> int:
    return int(cell, 16)


# --- Track / road-graph loading -------------------------------------------------------
@dataclass(frozen=True)
class Track:
    """One user's trajectory in every point representation."""
    latlon: tuple[tuple[float, float], ...]
    utm_mm: tuple[tuple[int, int], ...]     # scaled-integer UTM (millimetres)
    h3: tuple[str, ...]


def _track_from_latlon(points: list[tuple[float, float]]) -> Track:
    utm = [to_utm(lat, lon) for lat, lon in points]
    return Track(
        latlon=tuple(points),
        utm_mm=tuple((round(x * UTM_SCALE), round(y * UTM_SCALE)) for x, y in utm),
        h3=tuple(to_h3(lat, lon) for lat, lon in points),
    )


def _read_latlon_csv(path: Path) -> list[tuple[float, float]]:
    df = pd.read_csv(path)
    return [(float(r.lat), float(r.lon)) for r in df.itertuples()]


@lru_cache(maxsize=None)
def _load_track_cached(processed_dir: Path, user: str) -> Track:
    processed = processed_dir / "tracks" / f"{user}.csv"
    if processed.exists():
        df = pd.read_csv(processed)
        return Track(
            latlon=tuple((float(r.lat), float(r.lon)) for r in df.itertuples()),
            utm_mm=tuple((int(r.utm_x_mm), int(r.utm_y_mm)) for r in df.itertuples()),
            h3=tuple(str(r.h3) for r in df.itertuples()),
        )
    tracks_dir = processed_dir / "tracks"
    known = sorted(p.stem for p in tracks_dir.glob("*.csv")) if tracks_dir.is_dir() else []
    raise FileNotFoundError(
        f"no prepared track for user {user!r} in {tracks_dir} "
        f"(available: {', '.join(known) or 'none'}); download GeoLife and run "
        "`privela prepare-data` first (see README)"
    )


def load_track(site: SiteConfig, user: str) -> Track:
    """A user's prepared track from the processed directory."""
    track = _load_track_cached(site.processed_dir, user)
    if not track.latlon:
        raise ValueError(f"track of user {user!r} is empty")
    return track


def load_road_graph(site: SiteConfig) -> dict[str, Any]:
    """The prepared road subgraph: the processed directory first, else the bundled one."""
    path = site.processed_dir / "road" / "subgraph.json"
    if not path.exists():
        path = BUNDLED_ROAD_GRAPH
    g = json.loads(path.read_text())
    # Node tracks/sets are optional in the file; the defaults index the bundled subgraph.
    for key, default in _ROAD_DEFAULTS.items():
        g.setdefault(key, default)
    return g


# Node sequences/sets used by the road kernels (indices into the bundled subgraph).
_ROAD_DEFAULTS: dict[str, list[int]] = {
    "track_a": [0, 4, 10, 21, 1, 20],       # starts at the source node 0
    "track_b": [10, 21, 1, 30, 5],          # shares nodes 10, 21, 1 with track_a
    "waypoints": [10, 21, 1, 4, 20],
    "zone": [0, 4, 10, 21, 1],
    "candidates": [3, 7, 12, 19, 24, 30],
}


PTMP_MAX_C = 20
_CANDIDATE_USERS = ("010", "011", "000b")   # extra PTMP candidates (one each), if prepared


def _candidate_points(site: SiteConfig, alice: str, bob: str) -> list[tuple[Track, int]]:
    """PTMP candidate set: two near points from Alice's own track + other users' first points."""
    a = load_track(site, alice)
    picks = [(a, min(i, len(a.latlon) - 1)) for i in (2, 5)]
    picks.append((load_track(site, bob), 0))
    for user in _CANDIDATE_USERS:
        try:
            picks.append((load_track(site, user), 0))
        except FileNotFoundError:
            log.debug("candidate user %s not prepared; skipping", user)
    return picks


# --- Prepared inputs (shared by the input writer and the plaintext verifier) ----------
@dataclass
class Prepared:
    """Semantic inputs for one run, in plaintext, so the verifier can reproduce the result."""
    inputs: str
    payload: dict[str, Any]


# --- MP-SPDZ input file writing -------------------------------------------------------
def _player_data_dir(site: SiteConfig) -> Path:
    d = REPO_ROOT / "Player-Data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _public_input_path(spec: RepSpec) -> Path:
    d = REPO_ROOT / "Programs" / "Public-Input"
    d.mkdir(parents=True, exist_ok=True)
    return d / spec.program.split("/")[-1]   # MP-SPDZ looks up public input by basename


def _write_party(site: SiteConfig, party: int, values: list[int]) -> None:
    path = _player_data_dir(site) / f"Input-P{party}-0"
    path.write_text("".join(f"{v}\n" for v in values))


H3_MAX_RADIUS = 10   # disk capacity for the H3 distance/closer kernels (331 cells)
INF_MM = 10 ** 9


def write_inputs(spec: RepSpec, site: SiteConfig, *, alice: str, bob: str,
                 carol: str = "011") -> Prepared:
    """Write Player-Data / Public-Input for a (function, rep) and return the plaintext inputs.

    Dispatches on the named input layout in the manifest. ``alice``/``bob``/``carol`` select
    prepared users for the point-based layouts; road layouts use the prepared road subgraph.
    New layouts are added here as functions are added.
    """
    def p(user: str, rep: str):
        return _point(site, user, rep)

    layout = spec.inputs
    if layout == "two_points":
        return _write_two_points(spec, site, alice, bob)
    if layout == "two_points_d":
        ax, ay = p(alice, "utm")
        bx, by = p(bob, "utm")
        _write_party(site, 0, [ax, ay])
        _write_party(site, 1, [bx, by])
        return Prepared("two_points_d", {"alice_m": (ax, ay), "bob_m": (bx, by)})
    if layout == "three_points":
        px, py = p(alice, "utm")
        ax, ay = p(bob, "utm")
        bx, by = p(carol, "utm")
        _write_party(site, 0, [px, py])
        _write_party(site, 1, [ax, ay, bx, by])
        return Prepared("three_points", {"p_m": (px, py), "a_m": (ax, ay), "b_m": (bx, by)})
    if layout == "ring_and_cell":
        return _write_ring_and_cell(spec, site, alice, bob)
    if layout == "disk_and_cell":
        disk = h3_ordered_disk(p(alice, "h3"), H3_MAX_RADIUS)
        _write_party(site, 0, disk)
        _write_party(site, 1, [cell_to_int(p(bob, "h3"))])
        return Prepared("disk_and_cell", {"p_cell": p(alice, "h3"), "q_cell": p(bob, "h3"),
                                          "max_radius": H3_MAX_RADIUS})
    if layout == "disk_and_two_cells":
        disk = h3_ordered_disk(p(alice, "h3"), H3_MAX_RADIUS)
        _write_party(site, 0, disk)
        _write_party(site, 1, [cell_to_int(p(bob, "h3")), cell_to_int(p(carol, "h3"))])
        return Prepared("disk_and_two_cells", {"p_cell": p(alice, "h3"), "a_cell": p(bob, "h3"),
                                               "b_cell": p(carol, "h3"), "max_radius": H3_MAX_RADIUS})
    if layout == "two_nodes":
        return _write_two_nodes(spec, site)
    if layout in ("two_nodes_d", "src_two_targets"):
        return _write_road_multi(spec, site, layout)
    if layout in ("utm_set", "utm_set_r"):
        return _write_utm_set(spec, site, alice, bob, layout.endswith("_r"))
    if layout in ("h3_set", "h3_set_r"):
        return _write_h3_set(spec, site, alice, bob, layout.endswith("_r"))
    if layout in ("road_set", "road_set_r"):
        return _write_road_set(spec, site, layout.endswith("_r"))
    if layout == "pt_traj_utm":
        return _write_pt_traj_utm(spec, site, alice)
    if layout == "pt_traj_h3":
        return _write_pt_traj_h3(spec, site, alice)
    if layout == "pt_traj_road":
        return _write_pt_traj_road(spec, site)
    if layout == "pt_two_traj_utm":
        return _write_pt_two_traj_utm(spec, site, alice, bob)
    if layout == "pt_two_traj_h3":
        return _write_pt_two_traj_h3(spec, site, alice, bob)
    if layout == "pt_two_traj_road":
        return _write_pt_two_traj_road(spec, site)
    if layout in ("tt_pair_utm", "tt_common_utm", "tt_zone_utm"):
        return _write_tt_utm(spec, site, alice, bob, layout)
    if layout in ("tt_pair_h3", "tt_common_h3", "tt_zone_h3"):
        return _write_tt_eq(spec, site, alice, bob, layout, "h3")
    if layout in ("tt_pair_road", "tt_common_road", "tt_zone_road"):
        return _write_tt_eq(spec, site, alice, bob, layout, "road")
    raise NotImplementedError(f"input layout {layout!r} not implemented yet")


def _point(site: SiteConfig, user: str, rep: str):
    """Scaled-UTM tuple (rep='utm') or H3 cell string (rep='h3') of a user's first point."""
    track = load_track(site, user)
    return track.utm_mm[0] if rep == "utm" else track.h3[0]


def _write_two_points(spec: RepSpec, site: SiteConfig, alice: str, bob: str) -> Prepared:
    ax, ay = _point(site, alice, "utm")
    bx, by = _point(site, bob, "utm")
    r_scaled = round(spec.params["radius_m"] * UTM_SCALE)
    _write_party(site, 0, [ax, ay])
    _write_party(site, 1, [bx, by])
    _public_input_path(spec).write_text(f"{r_scaled * r_scaled}\n")
    return Prepared("two_points", {"alice_m": (ax, ay), "bob_m": (bx, by),
                                   "radius_m": spec.params["radius_m"]})


def _write_ring_and_cell(spec: RepSpec, site: SiteConfig, alice: str, bob: str) -> Prepared:
    hops = spec.params["hops"]
    max_ring = 1 + 3 * hops * (hops + 1)
    alice_cell = _point(site, alice, "h3")
    bob_cell = _point(site, bob, "h3")
    ring = [cell_to_int(c) for c in h3_disk_cells(alice_cell, hops)]
    ring_size = min(len(ring), max_ring)
    padded = (ring + [0] * max_ring)[:max_ring]
    _write_party(site, 0, [ring_size, *padded])
    _write_party(site, 1, [cell_to_int(bob_cell)])
    return Prepared("ring_and_cell", {"alice_cell": alice_cell, "bob_cell": bob_cell, "hops": hops})


def _road_matrix(g: dict, max_nodes: int) -> list[list[int]]:
    matrix = [[INF_MM] * max_nodes for _ in range(max_nodes)]
    for i in range(max_nodes):
        matrix[i][i] = 0
    for u, v, w in g["edges"]:
        matrix[u][v] = w
        matrix[v][u] = w
    return matrix


def _road_far_node(g: dict) -> int:
    """The reachable node farthest (shortest-path) from node_a -- a useful 'far' target."""
    import networkx as nx
    gr = nx.Graph()
    for u, v, w in g["edges"]:
        gr.add_edge(u, v, weight=w)
    lengths = nx.single_source_dijkstra_path_length(gr, g["node_a"], weight="weight")
    return max(lengths, key=lengths.get)


def _flat_public(spec: RepSpec, head: list[int], matrix: list[list[int]]) -> None:
    flat = head + [v for row in matrix for v in row]
    _public_input_path(spec).write_text("".join(f"{v}\n" for v in flat))


def _write_two_nodes(spec: RepSpec, site: SiteConfig) -> Prepared:
    g = load_road_graph(site)
    max_nodes = spec.params["max_nodes"]
    threshold_mm = round(spec.params["radius_m"] * UTM_SCALE)
    _write_party(site, 0, [g["node_a"]])
    _write_party(site, 1, [g["node_b"]])
    _flat_public(spec, [g["n_nodes"], threshold_mm], _road_matrix(g, max_nodes))
    return Prepared("two_nodes", {"edges": g["edges"], "n_nodes": g["n_nodes"],
                                  "node_a": g["node_a"], "node_b": g["node_b"],
                                  "threshold_mm": threshold_mm})


def _write_road_multi(spec: RepSpec, site: SiteConfig, layout: str) -> Prepared:
    g = load_road_graph(site)
    max_nodes = spec.params["max_nodes"]
    matrix = _road_matrix(g, max_nodes)
    if layout == "two_nodes_d":
        _write_party(site, 0, [g["node_a"]])
        _write_party(site, 1, [g["node_b"]])
        _flat_public(spec, [g["n_nodes"]], matrix)
        return Prepared("two_nodes_d", {"edges": g["edges"], "node_a": g["node_a"],
                                        "node_b": g["node_b"]})
    # src_two_targets: source=node_a, A=node_b (near), B=farthest node
    node_c = _road_far_node(g)
    _write_party(site, 0, [g["node_a"]])
    _write_party(site, 1, [g["node_b"], node_c])
    _flat_public(spec, [g["n_nodes"]], matrix)
    return Prepared("src_two_targets", {"edges": g["edges"], "node_p": g["node_a"],
                                        "node_a": g["node_b"], "node_b": node_c})


# --- PTMP layouts (query point/node + candidate set) ----------------------------------
def _utm_candidates(site: SiteConfig, alice: str, bob: str):
    cands = [t.utm_mm[i] for t, i in _candidate_points(site, alice, bob)]
    return _point(site, alice, "utm"), cands


def _write_utm_set(spec: RepSpec, site: SiteConfig, alice: str, bob: str,
                   with_radius: bool) -> Prepared:
    (qx, qy), cands = _utm_candidates(site, alice, bob)
    n = len(cands)
    p1 = [n]
    for i in range(PTMP_MAX_C):
        cx, cy = cands[i] if i < n else (0, 0)
        p1 += [cx, cy]
    _write_party(site, 0, [qx, qy])
    _write_party(site, 1, p1)
    payload = {"query": (qx, qy), "cands": cands, "k": spec.params.get("k")}
    if with_radius:
        r = round(spec.params["radius_m"] * UTM_SCALE)
        _public_input_path(spec).write_text(f"{r * r}\n")
        payload["radius_m"] = spec.params["radius_m"]
    return Prepared("utm_set_r" if with_radius else "utm_set", payload)


def _write_h3_set(spec: RepSpec, site: SiteConfig, alice: str, bob: str,
                  with_radius: bool) -> Prepared:
    p_cell = _point(site, alice, "h3")
    disk = h3_ordered_disk(p_cell, H3_MAX_RADIUS)
    cands = [t.h3[i] for t, i in _candidate_points(site, alice, bob)]
    n = len(cands)
    p1 = [n] + [cell_to_int(cands[i]) if i < n else 0 for i in range(PTMP_MAX_C)]
    _write_party(site, 0, disk)
    _write_party(site, 1, p1)
    payload = {"p_cell": p_cell, "cands": cands, "max_radius": H3_MAX_RADIUS, "k": spec.params.get("k")}
    if with_radius:
        hops = spec.params["hops"]
        _public_input_path(spec).write_text(f"{hops}\n")
        payload["hops"] = hops
    return Prepared("h3_set_r" if with_radius else "h3_set", payload)


def _write_road_set(spec: RepSpec, site: SiteConfig, with_radius: bool) -> Prepared:
    g = load_road_graph(site)
    matrix = _road_matrix(g, spec.params["max_nodes"])
    cands = g["candidates"][:PTMP_MAX_C]
    n = len(cands)
    p1 = [n] + [cands[i] if i < n else 0 for i in range(PTMP_MAX_C)]
    _write_party(site, 0, [g["node_a"]])
    _write_party(site, 1, p1)
    payload = {"edges": g["edges"], "node_p": g["node_a"], "cands": cands, "k": spec.params.get("k")}
    head = [g["n_nodes"]]
    if with_radius:
        thr = round(spec.params["radius_m"] * UTM_SCALE)
        head = [g["n_nodes"], thr]
        payload["threshold_mm"] = thr
    _flat_public(spec, head, matrix)
    return Prepared("road_set_r" if with_radius else "road_set", payload)


# --- PT layouts (query point/node + ordered trajectory) -------------------------------
PT_MAX_T = 20
PT_DT = 30          # sampling interval (seconds) assigned to the trajectory timestamps


def _utm_track(site: SiteConfig, user: str) -> list[tuple[int, int]]:
    return list(load_track(site, user).utm_mm[:PT_MAX_T])


def _h3_track(site: SiteConfig, user: str) -> list[str]:
    return list(load_track(site, user).h3[:PT_MAX_T])


def _pad_track(values_per_point: list[list[int]], width: int) -> list[int]:
    n = len(values_per_point)
    out = [n]
    for i in range(PT_MAX_T):
        out += values_per_point[i] if i < n else [0] * width
    return out


def _write_pt_traj_utm(spec, site, alice):
    qx, qy = _point(site, alice, "utm")
    track = _utm_track(site, alice)
    ts = [i * PT_DT for i in range(len(track))]
    p1 = _pad_track([[x, y, i * PT_DT] for i, (x, y) in enumerate(track)], 3)
    _write_party(site, 0, [qx, qy])
    _write_party(site, 1, p1)
    r = round(spec.params["radius_m"] * UTM_SCALE)
    delta = spec.params["delta_s"]
    _public_input_path(spec).write_text(f"{r * r}\n{delta}\n")
    return Prepared("pt_traj_utm", {"query": (qx, qy), "track": track, "ts": ts,
                                    "radius_m": spec.params["radius_m"], "delta": delta})


def _write_pt_traj_h3(spec, site, alice):
    p_cell = _point(site, alice, "h3")
    disk = h3_ordered_disk(p_cell, H3_MAX_RADIUS)
    track = _h3_track(site, alice)
    ts = [i * PT_DT for i in range(len(track))]
    p1 = _pad_track([[cell_to_int(c), i * PT_DT] for i, c in enumerate(track)], 2)
    _write_party(site, 0, disk)
    _write_party(site, 1, p1)
    delta = spec.params["delta_s"]
    _public_input_path(spec).write_text(f"{spec.params['hops']}\n{delta}\n")
    return Prepared("pt_traj_h3", {"p_cell": p_cell, "track": track, "ts": ts,
                                   "max_radius": H3_MAX_RADIUS, "hops": spec.params["hops"],
                                   "delta": delta})


def _write_pt_traj_road(spec, site):
    g = load_road_graph(site)
    matrix = _road_matrix(g, spec.params["max_nodes"])
    track = g["track_a"][:PT_MAX_T]
    ts = [i * PT_DT for i in range(len(track))]
    p1 = _pad_track([[node, i * PT_DT] for i, node in enumerate(track)], 2)
    _write_party(site, 0, [g["node_a"]])
    _write_party(site, 1, p1)
    thr = round(spec.params["radius_m"] * UTM_SCALE)
    delta = spec.params["delta_s"]
    _flat_public(spec, [g["n_nodes"], thr, delta], matrix)
    return Prepared("pt_traj_road", {"edges": g["edges"], "node_p": g["node_a"], "track": track,
                                     "ts": ts, "threshold_mm": thr, "delta": delta})


def _pad_set(values_per_point: list[list[int]], width: int) -> list[int]:
    n = len(values_per_point)
    out = [n]
    for i in range(PT_MAX_T):
        out += values_per_point[i] if i < n else [0] * width
    return out


def _write_pt_two_traj_utm(spec, site, alice, bob):
    qx, qy = _point(site, alice, "utm")
    ta, tb = _utm_track(site, alice), _utm_track(site, bob)
    _write_party(site, 0, [qx, qy])
    _write_party(site, 1, _pad_set([[x, y] for x, y in ta], 2) + _pad_set([[x, y] for x, y in tb], 2))
    return Prepared("pt_two_traj_utm", {"query": (qx, qy), "track_a": ta, "track_b": tb})


def _write_pt_two_traj_h3(spec, site, alice, bob):
    p_cell = _point(site, alice, "h3")
    disk = h3_ordered_disk(p_cell, H3_MAX_RADIUS)
    ta, tb = _h3_track(site, alice), _h3_track(site, bob)
    _write_party(site, 0, disk)
    _write_party(site, 1, _pad_set([[cell_to_int(c)] for c in ta], 1)
                          + _pad_set([[cell_to_int(c)] for c in tb], 1))
    return Prepared("pt_two_traj_h3", {"p_cell": p_cell, "track_a": ta, "track_b": tb,
                                       "max_radius": H3_MAX_RADIUS})


def _write_pt_two_traj_road(spec, site):
    g = load_road_graph(site)
    matrix = _road_matrix(g, spec.params["max_nodes"])
    ta = g["track_a"][:PT_MAX_T]           # passes through the source node (min distance 0)
    tb = [_road_far_node(g)]               # a far node
    _write_party(site, 0, [g["node_a"]])
    _write_party(site, 1, _pad_set([[n] for n in ta], 1) + _pad_set([[n] for n in tb], 1))
    _flat_public(spec, [g["n_nodes"]], matrix)
    return Prepared("pt_two_traj_road", {"edges": g["edges"], "node_p": g["node_a"],
                                         "track_a": ta, "track_b": tb})


# --- TT layouts (two trajectories / waypoints / zone) ---------------------------------
TT_MAX_T = 20


def _tt_pad(points: list, width: int) -> list[int]:
    n = len(points)
    out = [n]
    for i in range(TT_MAX_T):
        if i < n:
            v = points[i]
            out += list(v) if isinstance(v, (tuple, list)) else [v]
        else:
            out += [0] * width
    return out


def _write_tt_utm(spec, site, alice, bob, layout):
    ta, tb = _utm_track(site, alice), _utm_track(site, bob)
    if layout == "tt_zone_utm":
        xs = [x for x, _ in ta]
        ys = [y for _, y in ta]
        box = (min(xs), max(xs), min(ys), max(ys))
        _public_input_path(spec).write_text("".join(f"{v}\n" for v in box))
        _write_party(site, 0, _tt_pad(ta, 2))
        _write_party(site, 1, [])
        return Prepared("tt_zone_utm", {"a": ta, "b": tb, "bbox": box})
    r = round(spec.params["radius_m"] * UTM_SCALE)
    payload = {"a": ta, "b": tb, "radius_m": spec.params["radius_m"]}
    if layout == "tt_common_utm":
        wpts = ta[:5]
        head = [r * r] + [v for w in wpts for v in w]
        _public_input_path(spec).write_text("".join(f"{v}\n" for v in head))
        payload["waypoints"] = wpts
    else:
        _public_input_path(spec).write_text(f"{r * r}\n")
    _write_party(site, 0, _tt_pad(ta, 2))
    _write_party(site, 1, _tt_pad(tb, 2))
    return Prepared("tt_pair_utm" if layout == "tt_pair_utm" else "tt_common_utm", payload)


def _write_tt_eq(spec, site, alice, bob, layout, rep):
    if rep == "h3":
        ta = _h3_track(site, alice)
        tb = _h3_track(site, bob)
        ta_tok = [cell_to_int(c) for c in ta]
        tb_tok = [cell_to_int(c) for c in tb]
        waypoints = ta[:5]
        wp_tok = [cell_to_int(c) for c in waypoints]
        zone = ta[:5]
        zone_tok = wp_tok
    else:  # road
        g = load_road_graph(site)
        ta = ta_tok = g["track_a"][:TT_MAX_T]
        tb = tb_tok = g["track_b"][:TT_MAX_T]
        waypoints = wp_tok = g["waypoints"]
        zone = zone_tok = g["zone"]
    suffix = rep
    payload = {"a": ta, "b": tb}
    if layout.endswith("zone_" + suffix) or layout == f"tt_zone_{suffix}":
        _public_input_path(spec).write_text("".join(f"{v}\n" for v in zone_tok))
        _write_party(site, 0, _tt_pad(ta_tok, 1))
        _write_party(site, 1, [])
        payload["zone"] = zone
        return Prepared(f"tt_zone_{suffix}", payload)
    if layout == f"tt_common_{suffix}":
        _public_input_path(spec).write_text("".join(f"{v}\n" for v in wp_tok))
        payload["waypoints"] = waypoints
    _write_party(site, 0, _tt_pad(ta_tok, 1))
    _write_party(site, 1, _tt_pad(tb_tok, 1))
    return Prepared(layout, payload)


# --- prepare-data ---------------------------------------------------------------------
MAX_TRACK_POINTS = 15       # points kept per user (the trajectory kernels hold up to 20)
MIN_TRACK_POINTS = 5        # the TT kernels read 5 public waypoint / zone tokens
PLT_STRIDE = 3              # keep every 3rd GeoLife fix
SAMPLE_USERS = ("000", "009", "010", "011")   # GeoLife users of the sample used in the paper
SAMPLE_ALIAS = ("000b", "000", 9, 12)         # extra track: user 000 from fix 9, 12 points
PLT_HEADER_LINES = 6

ROAD_FETCH_DIST_M = 2000    # OSM download radius around Alice's first point
ROAD_MAX_NODES = 50         # capacity of the road kernels' public adjacency matrix
ROAD_MATCH_POINTS = 300     # raw GPS fixes map-matched per user
ROAD_NODE_B_DIST_M = 450    # fallback target distance for node_b
# Leuven HMM map-matching parameters (metres).
HMM_MAX_DIST = 200
HMM_MAX_DIST_INIT = 50
HMM_OBS_NOISE = 50

OSM_ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL 1.0"


def _read_plt(path: Path) -> list[tuple[float, float]]:
    df = pd.read_csv(path, skiprows=PLT_HEADER_LINES, header=None, usecols=[0, 1],
                     names=["lat", "lon"])
    return [(float(r.lat), float(r.lon)) for r in df.itertuples()]


def discover_raw(raw_dir: Path) -> dict[str, Path]:
    """Map user id -> raw source under ``raw_dir``.

    Accepts plain ``<user>.csv`` files (``lat,lon`` header) and the GeoLife layout
    ``[Geolife Trajectories 1.3/][Data/]<user>/Trajectory/*.plt``.
    """
    sources: dict[str, Path] = {p.stem: p for p in sorted(raw_dir.glob("*.csv"))}
    # the GeoLife archive unpacks to "<archive name>/Data/<user>/Trajectory"
    root = next((d for d in (raw_dir / "Data", *sorted(raw_dir.glob("*/Data"))) if d.is_dir()),
                raw_dir)
    for user_dir in sorted(d for d in root.iterdir() if (d / "Trajectory").is_dir()):
        sources.setdefault(user_dir.name, user_dir / "Trajectory")
    return sources


def _read_raw(source: Path) -> tuple[list[tuple[float, float]], int]:
    """All GPS fixes of a user's first usable trajectory, and the default subsampling stride."""
    if source.is_file():
        return _read_latlon_csv(source), 1
    for plt in sorted(source.glob("*.plt")):
        points = _read_plt(plt)
        if len(points) >= MIN_TRACK_POINTS * PLT_STRIDE:
            return points, PLT_STRIDE
    raise ValueError(f"no trajectory with at least {MIN_TRACK_POINTS * PLT_STRIDE} fixes in {source}")


def _write_track(processed_dir: Path, user: str, track: Track) -> None:
    out = processed_dir / "tracks" / f"{user}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "lat": [p[0] for p in track.latlon],
        "lon": [p[1] for p in track.latlon],
        "utm_x_mm": [p[0] for p in track.utm_mm],
        "utm_y_mm": [p[1] for p in track.utm_mm],
        "h3": list(track.h3),
    }).to_csv(out, index=False)


def prepare(site: SiteConfig, *, raw_dir: Path | None = None, users: list[str] | None = None,
            reps: list[str] | None = None, stride: int | None = None,
            max_points: int = MAX_TRACK_POINTS) -> None:
    """Convert raw GPS tracks under ``raw_dir`` into ``site.processed_dir``.

    UTM and H3 are always written (one ``tracks/<user>.csv`` per user). ``road`` additionally
    downloads the OSM road network around the first user and map-matches the first two users
    onto it; it needs the ``roadprep`` extra and network access.
    """
    raw_dir = raw_dir or site.data_dir / "raw"
    reps = reps or ["utm", "h3"]
    unknown = sorted(set(reps) - {"utm", "h3", "road"})
    if unknown:
        raise ValueError(f"unknown representation(s): {', '.join(unknown)}")
    if not raw_dir.is_dir():
        raise FileNotFoundError(
            f"raw data directory {raw_dir} not found; download and unpack GeoLife into it "
            "(see README) or pass --raw"
        )
    if not MIN_TRACK_POINTS <= max_points <= PT_MAX_T:
        raise ValueError(f"max_points must be within [{MIN_TRACK_POINTS}, {PT_MAX_T}]")

    sources = discover_raw(raw_dir)
    missing = [u for u in users or [] if u not in sources]
    if missing:
        raise FileNotFoundError(f"user(s) not found under {raw_dir}: {', '.join(missing)}")
    selected = users or sorted(sources)
    if not selected:
        raise FileNotFoundError(f"no <user>.csv or <user>/Trajectory/*.plt found under {raw_dir}")

    raw_points: dict[str, list[tuple[float, float]]] = {}
    for user in selected:
        try:
            points, default_stride = _read_raw(sources[user])
        except ValueError as e:
            log.warning("skipping user %s: %s", user, e)
            continue
        kept = points[::stride or default_stride][:max_points]
        if len(kept) < MIN_TRACK_POINTS:
            log.warning("skipping user %s: only %d points after subsampling", user, len(kept))
            continue
        raw_points[user] = points
        _write_track(site.processed_dir, user, _track_from_latlon(kept))
        log.info("  %s: %d points -> UTM + H3", user, len(kept))
    _load_track_cached.cache_clear()
    if not raw_points:
        raise ValueError("no usable trajectory found")

    if "road" in reps:
        prepared = list(raw_points)
        if len(prepared) < 2:
            raise ValueError("the road representation needs at least two users")
        graph = build_road_subgraph(raw_points[prepared[0]], raw_points[prepared[1]],
                                    users=(prepared[0], prepared[1]))
        out = site.processed_dir / "road" / "subgraph.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(graph, indent=2))
        log.info("  road: subgraph with %d nodes, %d edges (users %s, %s)",
                 graph["n_nodes"], len(graph["edges"]), prepared[0], prepared[1])
    log.info("Prepared %d user(s) under %s", len(raw_points), site.processed_dir)


def prepare_sample(site: SiteConfig, *, raw_dir: Path | None = None) -> None:
    """Prepare the small sample: the first trajectory of four GeoLife users, 15 points each."""
    raw_dir = raw_dir or site.data_dir / "raw"
    prepare(site, raw_dir=raw_dir, users=list(SAMPLE_USERS))
    alias, user, offset, count = SAMPLE_ALIAS
    points, stride = _read_raw(discover_raw(raw_dir)[user])
    _write_track(site.processed_dir, alias, _track_from_latlon(points[offset::stride][:count]))
    _load_track_cached.cache_clear()
    log.info("  %s: %d points -> UTM + H3 (second excerpt of user %s)", alias, count, user)


# --- Road representation (OSM download + HMM map-matching) ----------------------------
def _map_match(graph, points_xy: list[tuple[float, float]]) -> list:
    """Node sequence of the HMM map-matched path of projected GPS points (may be empty)."""
    from leuvenmapmatching.map.inmem import InMemMap
    from leuvenmapmatching.matcher.distance import DistanceMatcher

    logging.getLogger("leuvenmapmatching").setLevel(logging.WARNING)
    road_map = InMemMap("road", use_latlon=False, use_rtree=True, index_edges=True)
    for node, attrs in graph.nodes(data=True):
        # leuvenmapmatching stores projected coordinates as (y, x)
        road_map.add_node(node, (attrs["y"], attrs["x"]))
    for u, v in graph.edges():
        road_map.add_edge(u, v)
        road_map.add_edge(v, u)
    matcher = DistanceMatcher(road_map, max_dist=HMM_MAX_DIST, max_dist_init=HMM_MAX_DIST_INIT,
                              obs_noise=HMM_OBS_NOISE, non_emitting_states=True)
    matcher.match([(y, x) for x, y in points_xy])
    return list(matcher.path_pred_onlynodes)


def _dedupe(seq: list[int]) -> list[int]:
    return [v for i, v in enumerate(seq) if i == 0 or v != seq[i - 1]]


def _exactly(nodes: list[int], fill: list[int], size: int) -> list[int]:
    """``size`` distinct nodes: ``nodes`` first, topped up from ``fill``."""
    out = list(dict.fromkeys(nodes + fill))[:size]
    if len(out) < size:
        raise ValueError(f"road subgraph too small: need {size} distinct nodes, have {len(out)}")
    return out


def build_road_subgraph(points_a: list[tuple[float, float]], points_b: list[tuple[float, float]],
                        *, users: tuple[str, str], max_nodes: int = ROAD_MAX_NODES) -> dict[str, Any]:
    """Build the road subgraph and node tracks used by the road kernels.

    Downloads the drivable OSM network around Alice's first point, keeps the BFS neighbourhood
    of her nearest node (``max_nodes`` nodes, re-indexed from 0) and map-matches both users'
    GPS tracks onto it. Edge weights are road lengths in millimetres.
    """
    try:
        import networkx as nx
        import osmnx as ox
    except ImportError as e:
        raise RuntimeError(
            "the road representation needs the optional dependencies: `uv sync --extra roadprep`"
        ) from e

    lat0, lon0 = points_a[0]
    osm = ox.project_graph(
        ox.graph_from_point((lat0, lon0), dist=ROAD_FETCH_DIST_M, network_type="drive"),
        to_crs=UTM_CRS,
    )
    roads = nx.Graph()
    for u, v, attrs in osm.edges(data=True):
        if u != v and attrs["length"] < roads.get_edge_data(u, v, {}).get("length", float("inf")):
            roads.add_edge(u, v, length=float(attrs["length"]))
    for node in roads.nodes:
        roads.nodes[node].update(x=osm.nodes[node]["x"], y=osm.nodes[node]["y"])

    x0, y0 = to_utm(lat0, lon0)
    source = min(roads.nodes, key=lambda n: (roads.nodes[n]["x"] - x0) ** 2
                                            + (roads.nodes[n]["y"] - y0) ** 2)
    order: list = []
    queue, seen = deque([source]), {source}
    while queue and len(order) < max_nodes:
        node = queue.popleft()
        order.append(node)
        for nb in sorted(roads.neighbors(node)):
            if nb not in seen:
                seen.add(nb)
                queue.append(nb)
    index = {node: i for i, node in enumerate(order)}
    sub = roads.subgraph(order)
    edges = sorted([index[u], index[v], round(attrs["length"] * UTM_SCALE)]
                   for u, v, attrs in sub.edges(data=True))
    dist_mm = nx.single_source_dijkstra_path_length(
        nx.Graph((u, v, {"w": w}) for u, v, w in edges), 0, weight="w")
    far = max(dist_mm, key=dist_mm.get)

    def matched_track(points: list[tuple[float, float]], label: str) -> list[int]:
        xy = [to_utm(lat, lon) for lat, lon in points[:ROAD_MATCH_POINTS]]
        try:
            path = _map_match(roads, xy)
        except Exception as e:  # noqa: BLE001 - the matcher raises library-specific errors
            log.warning("map-matching failed for user %s: %s", label, e)
            path = []
        return _dedupe([index[n] for n in path if n in index])[:PT_MAX_T]

    track_a = matched_track(points_a, users[0])
    track_b = matched_track(points_b, users[1])
    relabelled = nx.relabel_nodes(sub, index)
    if len(track_a) < 2:
        log.warning("user %s has no map-matched path inside the subgraph; using a shortest path",
                    users[0])
        track_a = nx.shortest_path(relabelled, 0, far, weight="length")[:PT_MAX_T]
    if len(track_b) < 2:
        log.warning("user %s has no map-matched path inside the subgraph; using a shortest path",
                    users[1])
        mid = min(dist_mm, key=lambda n: abs(dist_mm[n] - dist_mm[far] / 2))
        track_b = nx.shortest_path(relabelled, far, mid, weight="length")[:PT_MAX_T]

    target_mm = ROAD_NODE_B_DIST_M * UTM_SCALE
    node_b = next((n for n in track_b if n != 0),
                  min((n for n in dist_mm if n != 0), key=lambda n: abs(dist_mm[n] - target_mm)))
    by_distance = sorted((n for n in dist_mm if n != 0), key=dist_mm.get)
    step = max(1, len(by_distance) // 6)
    return {
        "description": (
            f"OSM road subgraph ({UTM_CRS}): BFS neighbourhood of the node nearest user "
            f"{users[0]}'s first GPS point; tracks are HMM map-matched GeoLife trajectories."
        ),
        "attribution": OSM_ATTRIBUTION,
        "source": {"users": list(users), "latlon": [lat0, lon0],
                   "node_osmids": [str(n) for n in order]},
        "n_nodes": len(order),
        "max_nodes": max_nodes,
        "node_a": 0,
        "node_b": node_b,
        "edges": edges,
        "track_a": track_a,
        "track_b": track_b,
        "waypoints": _exactly(track_a, by_distance, 5),
        "zone": _exactly(track_a, by_distance, 5),
        "candidates": by_distance[::step][:6],
    }
