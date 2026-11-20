"""Shared trajectory-to-trajectory circuits over a cross-pair match.

Every TT function reduces to a per-pair *match* predicate plus a combinator. The match is
representation-specific:

    UTM   match(a, b) := d(a, b)^2 <= R^2     (Euclidean threshold, arithmetic)
    H3    match(a, b) := a_cell == b_cell      (cell co-location, equality)
    road  match(a, b) := a_node == b_node      (intersection co-location, equality)

Kernels build the A x B match matrix with the appropriate builder, then call a combinator
(count / any / LCSS / Jaccard). Endpoints, CommonPOIs and CountInZone use the same match
against specific points / public waypoints / a public zone.
"""

from Compiler.types import sint, Matrix, MemValue
from Compiler.library import for_range, for_range_opt


# --- match matrix builders ------------------------------------------------------------
def match_matrix_dist(ax, ay, na, bx, by, nb, r2):
    """UTM: M[i][j] = 1 iff a_i and b_j are within sqrt(r2), validity-masked."""
    A, B = len(ax), len(bx)
    M = Matrix(A, B, sint)
    @for_range_opt(A)
    def _(i):
        vi = sint(i) < na
        @for_range_opt(B)
        def _(j):
            d2 = (ax[i] - bx[j]) ** 2 + (ay[i] - by[j]) ** 2
            M[i][j] = (d2 <= r2) * vi * (sint(j) < nb)
    return M


def match_matrix_eq(a, na, b, nb):
    """H3 / road: M[i][j] = 1 iff a_i == b_j (same cell / node), validity-masked."""
    A, B = len(a), len(b)
    M = Matrix(A, B, sint)
    @for_range_opt(A)
    def _(i):
        vi = sint(i) < na
        @for_range_opt(B)
        def _(j):
            M[i][j] = (a[i] == b[j]) * vi * (sint(j) < nb)
    return M


# --- combinators over a match matrix --------------------------------------------------
def count_matches(M):
    c = MemValue(sint(0))
    @for_range(M.sizes[0])
    def _(i):
        @for_range(M.sizes[1])
        def _(j):
            c.iadd(M[i][j])
    return c.read()


def any_match(M):
    return count_matches(M) > 0


def lcss(M):
    """Longest common subsequence length under the match matrix (DP with secure max)."""
    A, B = M.sizes[0], M.sizes[1]
    L = Matrix(A + 1, B + 1, sint)
    @for_range(B + 1)
    def _(j):
        L[0][j] = sint(0)
    @for_range(A + 1)
    def _(i):
        L[i][0] = sint(0)

    @for_range(A)
    def _(i):
        @for_range(B)
        def _(j):
            up, left = L[i][j + 1], L[i + 1][j]
            best = (up > left).if_else(up, left)
            L[i + 1][j + 1] = M[i][j].if_else(L[i][j] + 1, best)
    return L[A][B]


def jaccard(M, na, nb):
    """Return (intersection, union): intersection = #A-points matched to some B-point;
    union = na + nb - intersection. Client divides to get the ratio."""
    inter = MemValue(sint(0))
    @for_range(M.sizes[0])
    def _(i):
        row = MemValue(sint(0))
        @for_range(M.sizes[1])
        def _(j):
            row.iadd(M[i][j])
        inter.iadd((sint(i) < na) * (row.read() > 0))
    iv = inter.read()
    return iv, na + nb - iv


def oblivious_at(arr, idx):
    """arr[idx] for a secret idx."""
    out = sint(0)
    @for_range_opt(len(arr))
    def _(i):
        out.update(out + (sint(i) == idx) * arr[i])
    return out


# --- CommonPOIs (public waypoints) and CountInZone (public zone) ----------------------
def common_count_eq(a, na, b, nb, waypoints):
    """Number of public waypoints (cells/nodes) visited by both trajectories (equality)."""
    total = MemValue(sint(0))
    for w in waypoints:
        in_a = MemValue(sint(0))
        @for_range(len(a))
        def _(i):
            in_a.iadd((a[i] == w) * (sint(i) < na))
        in_b = MemValue(sint(0))
        @for_range(len(b))
        def _(j):
            in_b.iadd((b[j] == w) * (sint(j) < nb))
        total.iadd((in_a.read() > 0) * (in_b.read() > 0))
    return total.read()


def common_count_dist(ax, ay, na, bx, by, nb, waypoints, r2):
    """Number of public (x,y) waypoints within R of a point on each trajectory (UTM)."""
    total = MemValue(sint(0))
    for wx, wy in waypoints:
        in_a = MemValue(sint(0))
        @for_range(len(ax))
        def _(i):
            in_a.iadd(((ax[i] - wx) ** 2 + (ay[i] - wy) ** 2 <= r2) * (sint(i) < na))
        in_b = MemValue(sint(0))
        @for_range(len(bx))
        def _(j):
            in_b.iadd(((bx[j] - wx) ** 2 + (by[j] - wy) ** 2 <= r2) * (sint(j) < nb))
        total.iadd((in_a.read() > 0) * (in_b.read() > 0))
    return total.read()


def count_in_set(a, na, zone):
    """Number of trajectory points whose cell/node is in the public zone set."""
    c = MemValue(sint(0))
    @for_range(len(a))
    def _(i):
        in_z = MemValue(sint(0))
        for z in zone:
            in_z.iadd(a[i] == z)
        c.iadd((sint(i) < na) * (in_z.read() > 0))
    return c.read()


def count_in_bbox(ax, ay, na, xmin, xmax, ymin, ymax):
    """Number of UTM trajectory points inside the public bounding box."""
    c = MemValue(sint(0))
    @for_range(len(ax))
    def _(i):
        inside = (ax[i] >= xmin) * (ax[i] <= xmax) * (ay[i] >= ymin) * (ay[i] <= ymax)
        c.iadd((sint(i) < na) * inside)
    return c.read()
