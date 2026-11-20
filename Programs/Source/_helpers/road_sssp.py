"""Shared road-network circuit: secure single-source shortest path with a secret shuffle.

Implements the privacy-preserving Dijkstra of Ehrmanntraut & Meyer, "Going Faster:
Privacy-Preserving Shortest Paths from Start to End" (2025):

  * the public adjacency matrix is secret-permuted (rows + columns) so node identities are
    hidden behind a random permutation;
  * single-source Dijkstra runs from A; A's permuted start and the per-iteration argmin index
    are revealed (admissible under the random permutation), the visited set stays secret;
  * B's position is looked up obliviously via a one-hot selector and is never revealed.

Returns the secret distance dist(A, B), from which a kernel derives its result (a threshold
test for IsInRadius, a comparison for IsCloser, the raw value for Distance).

Imported by road ``.mpc`` programs; uses MP-SPDZ Compiler types directly.
"""

import math

from Compiler.types import sint, regint, Array
from Compiler.library import for_range, for_range_opt, if_
from Compiler.util import tree_reduce

INF = 10 ** 9


def _argmin_tree(values):
    def op(a, b):
        idx_a, val_a = a
        idx_b, val_b = b
        a_smaller = val_a <= val_b
        return (a_smaller.if_else(idx_a, idx_b), a_smaller.if_else(val_a, val_b))

    indexed = [(sint(i), values[i]) for i in range(len(values))]
    return tree_reduce(op, indexed)


def _demux(x, n):
    """One-hot vector of length n with a 1 at secret position x (O(log log n) products)."""
    bits = list(reversed(x.bit_decompose(int(math.ceil(math.log2(max(n, 2)))))))
    return _demux_rec(bits, n)


def _demux_rec(bits, n):
    if len(bits) == 1:
        out = sint.Array(min(2, n))
        out[0] = 1 - bits[0]
        if n > 1:
            out[1] = bits[0]
        return out
    mid = len(bits) // 2
    left = _demux_rec(bits[:mid], 2 ** mid)
    right = _demux_rec(bits[mid:], 2 ** (len(bits) - mid))
    size = min(len(left) * len(right), n)
    out = sint.Array(size)
    idx = 0
    for i in range(len(left)):
        for j in range(len(right)):
            if idx < size:
                out[idx] = left[i] * right[j]
            idx += 1
    return out


def _transpose(m, n):
    t = sint.Matrix(n, n)
    @for_range_opt(n)
    def _(i):
        @for_range_opt(n)
        def _(j):
            t[i][j] = m[j][i]
    return t


def _permute_matrix(m, perm, n):
    """Permute rows and columns of an n x n secret matrix by the same secret shuffle.

    Matrix.secure_permute permutes rows; columns are permuted by transposing (with an explicit
    element copy -- transpose() views alias the buffer and corrupt the second permute), permuting
    rows, and transposing back. Result: ``out[P(i)][P(j)] = m[i][j]``.
    """
    a = sint.Matrix(n, n)
    a.assign(m)
    a.secure_permute(perm)
    b = _transpose(a, n)
    b.secure_permute(perm)
    return _transpose(b, n)


def _onehot_array(x, max_nodes, perm):
    sel = _demux(x, max_nodes)
    arr = sint.Array(max_nodes)
    for i in range(max_nodes):
        arr[i] = sel[i] if i < len(sel) else sint(0)
    arr.secure_permute(perm)
    return arr


def _secret_lookup(arr, selector):
    out = sint(0)
    @for_range_opt(len(arr))
    def _(i):
        out.update(out + arr[i] * selector[i])
    return out


def dijkstra(adj_public, source, max_nodes):
    """Secure single-source shortest paths from ``source`` over a public adjacency matrix.

    Returns ``(dist, perm)`` where ``dist`` is the secret distance array indexed by *permuted*
    position and ``perm`` is the secret shuffle. Look up a target's distance with ``lookup``.

    ``adj_public`` is a ``max_nodes x max_nodes`` cint matrix (mm weights, INF off-graph,
    0 on the diagonal). The shuffle scatters real nodes across all positions, so node validity
    cannot be tested by position; padding nodes are isolated by INF edges, so a full Dijkstra
    over all ``max_nodes`` positions is correct.
    """
    perm = sint.get_secure_shuffle(max_nodes)

    adj = sint.Matrix(max_nodes, max_nodes)
    @for_range_opt(max_nodes)
    def _(i):
        @for_range_opt(max_nodes)
        def _(j):
            adj[i][j] = adj_public[i][j]
    adj = _permute_matrix(adj, perm, max_nodes)

    # Source's permuted start (revealed — admissible under the random permutation).
    sel_s = _onehot_array(source, max_nodes, perm)
    start = sint(0)
    @for_range_opt(max_nodes)
    def _(i):
        start.update(start + i * sel_s[i])
    start_idx = regint(start.reveal())

    dist = sint.Array(max_nodes)
    @for_range_opt(max_nodes)
    def _(i):
        dist[i] = INF
    dist[start_idx] = 0
    visited = sint.Array(max_nodes)
    visited.assign_all(0)

    # `current` must live in memory so it persists across the runtime for_range loop.
    current = regint.Array(1)
    current[0] = start_idx

    @for_range(max_nodes)
    def _(it):
        cur = current[0]

        @for_range_opt(max_nodes)
        def _(i):
            visited[i] = (i == cur).if_else(sint(1), visited[i])

        cur_dist = dist[cur]

        @for_range_opt(max_nodes)
        def _(j):
            proposed = cur_dist + adj[cur][j]
            should = (visited[j] == 0) * (proposed < dist[j])
            dist[j] = should.if_else(proposed, dist[j])

        @if_(it < max_nodes - 1)
        def _():
            sel = sint.Array(max_nodes)
            @for_range_opt(max_nodes)
            def _(i):
                sel[i] = dist[i] + visited[i] * (2 * INF)
            nxt, _v = _argmin_tree(sel)
            current[0] = regint(nxt.reveal())

    return dist, perm


def lookup(dist, target, perm, max_nodes):
    """Obliviously read ``dist`` at ``target``'s (secret) permuted position."""
    sel = _onehot_array(target, max_nodes, perm)
    return _secret_lookup(dist, sel)


def secure_distance(adj_public, node_a, node_b, max_nodes):
    """Secret shortest-path distance between ``node_a`` (source) and ``node_b`` (target)."""
    dist, perm = dijkstra(adj_public, node_a, max_nodes)
    return lookup(dist, node_b, perm, max_nodes)


def candidate_distances(dist, cands, perm, max_nodes):
    """Shortest-path distance from the Dijkstra source to each candidate node.

    ``cands`` is a fixed-length Array; the per-candidate lookup is unrolled at compile time.
    """
    raw = Array(len(cands), sint)
    for i in range(len(cands)):
        raw[i] = lookup(dist, cands[i], perm, max_nodes)
    return raw
