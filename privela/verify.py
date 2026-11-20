"""Plaintext reference implementations and the MPC-vs-plaintext correctness check.

Each function has a plaintext implementation per input layout that reproduces, in the clear,
exactly what the MPC kernel computes (UTM in scaled-integer millimetres, H3 in grid hops,
road in millimetres). ``check`` compares it against the value the MPC program revealed.
"""

from __future__ import annotations

from typing import Callable

from .config import RepSpec
from .data import UTM_SCALE, INF_MM, Prepared

# function name -> (input layout -> plaintext impl returning the revealed value)
_PLAINTEXT: dict[str, dict[str, Callable[[RepSpec, Prepared], int]]] = {}


def register(function: str, layout: str):
    def deco(fn: Callable[[RepSpec, Prepared], int]):
        _PLAINTEXT.setdefault(function, {})[layout] = fn
        return fn
    return deco


def plaintext_result(function: str, spec: RepSpec, prepared: Prepared) -> int:
    try:
        impl = _PLAINTEXT[function][prepared.inputs]
    except KeyError:
        raise NotImplementedError(
            f"no plaintext reference for function {function!r} / layout {prepared.inputs!r}"
        )
    return impl(spec, prepared)


def check(function: str, spec: RepSpec, prepared: Prepared, result) -> tuple[bool, object]:
    """Compare the MPC output against the plaintext reference.

    ``result`` is a Result; multi-value functions (e.g. Jaccard) return a list/tuple plaintext
    compared against all parsed RESULT values, scalars against the first.
    """
    expected = plaintext_result(function, spec, prepared)
    if isinstance(expected, (list, tuple)):
        got = result.extra.get("results", [])
        return (list(expected) == list(got), expected)
    return (expected == result.mpc_result, expected)


# --- shared helpers -------------------------------------------------------------------
def _sq_dist(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _grid_dist(c1: str, c2: str) -> int:
    import h3
    return h3.grid_distance(c1, c2)


def _road_lengths(edges, source):
    import networkx as nx
    g = nx.Graph()
    for u, v, w in edges:
        g.add_edge(u, v, weight=w)
    return nx.single_source_dijkstra_path_length(g, source, weight="weight")


# ======================================================================================
# is_within_radius
# ======================================================================================
@register("is_within_radius", "two_points")
def _iwr_utm(spec, p):
    r = round(p.payload["radius_m"] * UTM_SCALE)
    return int(_sq_dist(p.payload["alice_m"], p.payload["bob_m"]) <= r * r)


@register("is_within_radius", "ring_and_cell")
def _iwr_h3(spec, p):
    return int(_grid_dist(p.payload["alice_cell"], p.payload["bob_cell"]) <= p.payload["hops"])


@register("is_within_radius", "two_nodes")
def _iwr_road(spec, p):
    d = _road_lengths(p.payload["edges"], p.payload["node_a"]).get(p.payload["node_b"], INF_MM)
    return int(d <= p.payload["threshold_mm"])


# ======================================================================================
# distance
# ======================================================================================
@register("distance", "two_points_d")
def _dist_utm(spec, p):
    return _sq_dist(p.payload["alice_m"], p.payload["bob_m"])   # squared, scaled units


@register("distance", "disk_and_cell")
def _dist_h3(spec, p):
    gd = _grid_dist(p.payload["p_cell"], p.payload["q_cell"])
    return gd if gd <= p.payload["max_radius"] else -1


@register("distance", "two_nodes_d")
def _dist_road(spec, p):
    return _road_lengths(p.payload["edges"], p.payload["node_a"]).get(p.payload["node_b"], INF_MM)


# ======================================================================================
# is_closer_to_a_than_b
# ======================================================================================
@register("is_closer_to_a_than_b", "three_points")
def _closer_utm(spec, p):
    pa = _sq_dist(p.payload["p_m"], p.payload["a_m"])
    pb = _sq_dist(p.payload["p_m"], p.payload["b_m"])
    return int(pa < pb)


@register("is_closer_to_a_than_b", "disk_and_two_cells")
def _closer_h3(spec, p):
    out = p.payload["max_radius"] + 1
    ga = _grid_dist(p.payload["p_cell"], p.payload["a_cell"])
    gb = _grid_dist(p.payload["p_cell"], p.payload["b_cell"])
    da = ga if ga <= p.payload["max_radius"] else out
    db = gb if gb <= p.payload["max_radius"] else out
    return int(da < db)


@register("is_closer_to_a_than_b", "src_two_targets")
def _closer_road(spec, p):
    lengths = _road_lengths(p.payload["edges"], p.payload["node_p"])
    da = lengths.get(p.payload["node_a"], INF_MM)
    db = lengths.get(p.payload["node_b"], INF_MM)
    return int(da < db)


# ======================================================================================
# PTMP: query point/node + candidate set  (argmin / count / any / top-k)
# ======================================================================================
H3_BIG = 10 ** 6


def _dists_utm(p):
    return [_sq_dist(p.payload["query"], c) for c in p.payload["cands"]]


def _dists_h3(p):
    import h3
    out = []
    for c in p.payload["cands"]:
        gd = h3.grid_distance(p.payload["p_cell"], c)
        out.append(gd if gd <= p.payload["max_radius"] else H3_BIG)
    return out


def _dists_road(p):
    lengths = _road_lengths(p.payload["edges"], p.payload["node_p"])
    return [lengths.get(c, INF_MM) for c in p.payload["cands"]]


def _thr_utm(p):
    return round(p.payload["radius_m"] * UTM_SCALE) ** 2


_DISTS = {"utm_set": _dists_utm, "utm_set_r": _dists_utm,
          "h3_set": _dists_h3, "h3_set_r": _dists_h3,
          "road_set": _dists_road, "road_set_r": _dists_road}
_THR = {"utm_set_r": _thr_utm,
        "h3_set_r": lambda p: p.payload["hops"],
        "road_set_r": lambda p: p.payload["threshold_mm"]}


def _reg_ptmp(function, op):
    for layout, dists in _DISTS.items():
        if function in ("count_within_radius", "any_within_radius") and not layout.endswith("_r"):
            continue
        if function in ("argmin_distance", "topk_nearest") and layout.endswith("_r"):
            continue
        register(function, layout)(lambda spec, p, _d=dists, _op=op: _op(spec, p, _d(p)))


_reg_ptmp("argmin_distance", lambda spec, p, ds: min(range(len(ds)), key=lambda i: ds[i]))
_reg_ptmp("topk_nearest", lambda spec, p, ds: sorted(ds)[p.payload["k"] - 1])
_reg_ptmp("count_within_radius", lambda spec, p, ds: sum(d <= _THR[p.inputs](p) for d in ds))
_reg_ptmp("any_within_radius", lambda spec, p, ds: int(any(d <= _THR[p.inputs](p) for d in ds)))


# ======================================================================================
# PT: query point/node vs an ordered trajectory  (dwell / entry / is-closer)
# ======================================================================================
def _within_utm(p):
    r = round(p.payload["radius_m"] * UTM_SCALE)
    return [int(_sq_dist(p.payload["query"], pt) <= r * r) for pt in p.payload["track"]]


def _within_h3(p):
    import h3
    return [int(h3.grid_distance(p.payload["p_cell"], c) <= p.payload["hops"])
            for c in p.payload["track"]]


def _within_road(p):
    lengths = _road_lengths(p.payload["edges"], p.payload["node_p"])
    return [int(lengths.get(node, INF_MM) <= p.payload["threshold_mm"]) for node in p.payload["track"]]


_WITHIN = {"pt_traj_utm": _within_utm, "pt_traj_h3": _within_h3, "pt_traj_road": _within_road}


def _dwell(within, ts, delta):
    in_run = start = last = total = 0
    for w, t in zip(within, ts):
        if w and not in_run:
            start = t
        if w:
            last = t
        if (not w) and in_run and (last - start) >= delta:
            total += last - start
        in_run = w
    if in_run and (last - start) >= delta:
        total += last - start
    return total


def _entry(within):
    return sum((1 - within[i]) * within[i + 1] for i in range(len(within) - 1))


for _layout, _w in _WITHIN.items():
    register("dwell_duration", _layout)(
        lambda spec, p, _w=_w: _dwell(_w(p), p.payload["ts"], p.payload["delta"]))
    register("entry_count", _layout)(lambda spec, p, _w=_w: _entry(_w(p)))


def _min_to_track_utm(p, track):
    return min(_sq_dist(p.payload["query"], pt) for pt in track)


def _min_to_track_h3(p, track):
    import h3
    big = H3_BIG
    vals = [h3.grid_distance(p.payload["p_cell"], c) for c in track]
    return min(v if v <= p.payload["max_radius"] else big for v in vals)


def _min_to_track_road(p, track):
    lengths = _road_lengths(p.payload["edges"], p.payload["node_p"])
    return min(lengths.get(n, INF_MM) for n in track)


_MIN_TRACK = {"pt_two_traj_utm": _min_to_track_utm, "pt_two_traj_h3": _min_to_track_h3,
              "pt_two_traj_road": _min_to_track_road}

for _layout, _mt in _MIN_TRACK.items():
    register("is_closer_to_traj_a_than_b", _layout)(
        lambda spec, p, _mt=_mt: int(_mt(p, p.payload["track_a"]) < _mt(p, p.payload["track_b"])))


# ======================================================================================
# TT: two trajectories  (count-close / within / LCSS / Jaccard / endpoints / common / zone)
# ======================================================================================
def _rep_of(layout):
    return layout.rsplit("_", 1)[1]


def _match_fn(p):
    if _rep_of(p.inputs) == "utm":
        r2 = round(p.payload["radius_m"] * UTM_SCALE) ** 2
        return lambda x, y: _sq_dist(x, y) <= r2
    return lambda x, y: x == y


def _count_close(spec, p):
    m = _match_fn(p)
    return sum(1 for x in p.payload["a"] for y in p.payload["b"] if m(x, y))


def _lcss_tt(spec, p):
    a, b, m = p.payload["a"], p.payload["b"], _match_fn(p)
    L = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            L[i + 1][j + 1] = L[i][j] + 1 if m(a[i], b[j]) else max(L[i][j + 1], L[i + 1][j])
    return L[len(a)][len(b)]


def _jaccard_tt(spec, p):
    a, b, m = p.payload["a"], p.payload["b"], _match_fn(p)
    inter = sum(1 for x in a if any(m(x, y) for y in b))
    return [inter, len(a) + len(b) - inter]


def _endpoints_tt(spec, p):
    a, b, m = p.payload["a"], p.payload["b"], _match_fn(p)
    return int(bool(m(a[0], b[0])) and bool(m(a[-1], b[-1])))


def _common_tt(spec, p):
    a, b, m = p.payload["a"], p.payload["b"], _match_fn(p)
    return sum(1 for w in p.payload["waypoints"]
               if any(m(x, w) for x in a) and any(m(y, w) for y in b))


def _zone_tt(spec, p):
    if _rep_of(p.inputs) == "utm":
        xmin, xmax, ymin, ymax = p.payload["bbox"]
        return sum(1 for x, y in p.payload["a"] if xmin <= x <= xmax and ymin <= y <= ymax)
    zone = set(p.payload["zone"])
    return sum(1 for t in p.payload["a"] if t in zone)


for _rep in ("utm", "h3", "road"):
    register("count_close", f"tt_pair_{_rep}")(_count_close)
    register("trajectories_within_radius", f"tt_pair_{_rep}")(
        lambda spec, p: int(_count_close(spec, p) > 0))
    register("lcss", f"tt_pair_{_rep}")(_lcss_tt)
    register("jaccard", f"tt_pair_{_rep}")(_jaccard_tt)
    register("endpoints", f"tt_pair_{_rep}")(_endpoints_tt)
    register("common_pois", f"tt_common_{_rep}")(_common_tt)
    register("count_in_zone", f"tt_zone_{_rep}")(_zone_tt)
