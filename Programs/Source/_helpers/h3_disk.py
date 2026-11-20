"""Shared H3 circuit: secret membership of a cell in a precomputed disk (ring).

The H3 proximity primitive: party A pre-computes the public-radius disk around its cell
(all cell ids within ``max_hops`` rings) and secret-shares it, padded to a fixed size with a
secret ``ring_size``. Membership then reduces to a batched equality test.

Imported by H3 ``.mpc`` programs; uses MP-SPDZ Compiler types directly so it works the same
whether the caller is run through ``compile.py`` for any arithmetic protocol.
"""

from Compiler.types import sint, cint, Array, MemValue
from Compiler.library import for_range, for_range_parallel


def ring_size_for(max_hops):
    """Number of cells in an H3 disk of the given hop radius: 1 + 3*r*(r+1)."""
    return 1 + 3 * max_hops * (max_hops + 1)


def ring_labels(max_radius):
    """Public per-cell ring index for cells laid out ring-by-ring (0..max_radius)."""
    labels = Array(ring_size_for(max_radius), cint)
    idx = 0
    for d in range(max_radius + 1):
        for _ in range(1 if d == 0 else 6 * d):
            labels[idx] = cint(d)
            idx += 1
    return labels


def ring_distance(query_cell, ordered_cells, max_radius):
    """Grid (hop) distance of ``query_cell`` within a ring-ordered disk.

    ``ordered_cells`` lists all disk cells grouped by ring (ring 0, ring 1, ...). Returns
    ``(distance, found)``: ``distance`` is the secret hop count if the cell is in the disk,
    else -1; ``found`` is the secret membership bit.
    """
    n = len(ordered_cells)
    labels = ring_labels(max_radius)
    eq = Array(n, sint)

    @for_range_parallel(n, n)
    def _(j):
        eq[j] = query_cell == ordered_cells[j]

    found = MemValue(sint(0))
    dist = MemValue(sint(0))

    @for_range(n)
    def _(j):
        found.iadd(eq[j])              # sint + sint = local
        dist.iadd(eq[j] * labels[j])   # sint * cint = local

    was_found = found.read() > 0
    return was_found.if_else(dist.read(), sint(-1)), was_found


def candidate_distances(cands, ordered_cells, max_radius, big):
    """Ring distance from the disk owner to each candidate cell (``big`` if outside the disk).

    ``cands`` is a fixed-length Array; the per-candidate scan is unrolled at compile time
    because :func:`ring_distance` allocates its own working storage.
    """
    raw = Array(len(cands), sint)
    for i in range(len(cands)):
        dist, found = ring_distance(cands[i], ordered_cells, max_radius)
        raw[i] = found.if_else(dist, sint(big))
    return raw


def in_disk(query_cell, ring_cells, ring_size):
    """Return sint 1/0: is ``query_cell`` among the first ``ring_size`` of ``ring_cells``?

    Phase 1 batches all equality+validity tests (independent -> minimal rounds);
    phase 2 is a local sum. Result is the boolean "membership count > 0".
    """
    max_ring = len(ring_cells)
    valid_eq = Array(max_ring, sint)

    @for_range_parallel(max_ring, max_ring)
    def _(j):
        is_eq = query_cell == ring_cells[j]
        is_valid = sint(j) < ring_size
        valid_eq[j] = is_eq * is_valid

    total = MemValue(sint(0))

    @for_range(max_ring)
    def _(j):
        total.iadd(valid_eq[j])

    return total.read() > 0
