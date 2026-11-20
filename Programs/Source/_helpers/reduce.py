"""Shared reductions over a secret distance array (representation-agnostic).

PTMP kernels build a per-representation distance array ``d`` where invalid / out-of-range
candidates are masked to a large value, then call one of these reductions. Keeping the
reduction logic here avoids re-implementing argmin / count / k-th-smallest per representation.
"""

from Compiler.types import sint, Array, MemValue
from Compiler.library import for_range, for_range_opt
from Compiler.util import tree_reduce


def masked(values, n, big):
    """Array equal to ``values`` for indices < secret count ``n``, else ``big``."""
    out = Array(len(values), sint)
    @for_range_opt(len(values))
    def _(i):
        out[i] = (sint(i) < n).if_else(values[i], sint(big))
    return out


def argmin_index(d):
    """Secret index of the minimum of ``d`` (ties: lowest index)."""
    def op(a, b):
        ia, va = a
        ib, vb = b
        s = va <= vb
        return (s.if_else(ia, ib), s.if_else(va, vb))
    idx, _v = tree_reduce(op, [(sint(i), d[i]) for i in range(len(d))])
    return idx


def min_value(d):
    """Minimum value of ``d`` (entries should already be validity-masked)."""
    return tree_reduce(lambda a, b: (a <= b).if_else(a, b), [d[i] for i in range(len(d))])


def entry_count(within):
    """Number of outside->inside transitions along ``within`` (a validity-masked 0/1 array).

    Counts i with within[i] == 0 and within[i+1] == 1. Because padding points are masked to
    0, transitions into or within padding contribute nothing.
    """
    c = MemValue(sint(0))
    @for_range(len(within) - 1)
    def _(i):
        c.iadd((1 - within[i]) * within[i + 1])
    return c.read()


def dwell_sum(within, timestamps, delta):
    """Total duration of maximal in-radius runs of length >= delta (sequential scan).

    ``within`` is a validity-masked 0/1 array; ``timestamps`` the per-point times. A run's
    duration is last_in_time - run_start_time, added only if it is at least ``delta``.
    """
    n = len(within)
    in_run = MemValue(sint(0))
    start = MemValue(sint(0))
    last = MemValue(sint(0))
    total = MemValue(sint(0))

    @for_range(n)
    def _(i):
        w = within[i]
        t = timestamps[i]
        was = in_run.read()
        entering = w * (1 - was)
        start.write(entering * t + (1 - entering) * start.read())
        last.write(w * t + (1 - w) * last.read())
        dur = last.read() - start.read()
        exiting = (1 - w) * was
        total.write(total.read() + exiting * (dur >= delta) * dur)
        in_run.write(w)

    fd = last.read() - start.read()
    total.write(total.read() + in_run.read() * (fd >= delta) * fd)
    return total.read()


def count_le(d, threshold):
    """Secret count of entries ``<= threshold``."""
    c = MemValue(sint(0))
    @for_range(len(d))
    def _(i):
        c.iadd(d[i] <= threshold)
    return c.read()


def any_le(d, threshold):
    """Secret bit: is any entry ``<= threshold``?"""
    return count_le(d, threshold) > 0


def kth_smallest(d, k, big):
    """Value of the k-th smallest entry (1-indexed), via k oblivious min-extractions.

    ``k`` is public (compile-time), matching the public top-k parameter.
    """
    n = len(d)
    work = Array(n, sint)
    @for_range_opt(n)
    def _(i):
        work[i] = d[i]

    result = MemValue(sint(0))
    for kk in range(k):
        mv = MemValue(sint(big))
        mi = MemValue(sint(0))
        @for_range(n)
        def _(i):
            s = work[i] < mv.read()
            mv.write(s.if_else(work[i], mv.read()))
            mi.write(s.if_else(sint(i), mi.read()))
        if kk == k - 1:
            result.write(mv.read())
        @for_range_opt(n)
        def _(i):
            work[i] = (sint(i) == mi.read()).if_else(sint(big), work[i])
    return result.read()
