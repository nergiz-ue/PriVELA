"""PriVELA command-line interface: list | prepare-data | run | verify."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import data, runner, verify
from .config import DEFAULT_PROTOCOL, Manifest, get_protocol, load_site_config
from .results import Result, save


def _split(csv: str | None) -> list[str] | None:
    if not csv:
        return None
    return [x.strip() for x in csv.split(",") if x.strip()]


def _resolve_reps(manifest: Manifest, function: str, reps_arg: str | None) -> list[str]:
    available = list(manifest.get(function).reps)
    if not reps_arg or reps_arg == "all":
        return available
    chosen = _split(reps_arg) or []
    unknown = [r for r in chosen if r not in available]
    if unknown:
        sys.exit(f"function {function!r} has no representation(s): {', '.join(unknown)} "
                 f"(available: {', '.join(available)})")
    return chosen


def _matrix(manifest: Manifest, function: str, reps: list[str], protocols: list[str]):
    """Yield (spec, protocol) honouring per-(function,rep) protocols_skip."""
    for rep in reps:
        spec = manifest.resolve(function, rep)
        for proto in protocols:
            get_protocol(proto)  # validate name
            if proto in spec.protocols_skip:
                continue
            yield spec, proto


# --- commands -------------------------------------------------------------------------
def cmd_list(args) -> int:
    manifest = Manifest.load()
    for fname, fspec in sorted(manifest.functions.items()):
        print(f"{fname}  [{fspec.cls}]")
        for rep, spec in fspec.reps.items():
            print(f"    {rep:5s} -> {spec.program}   protocols: {', '.join(spec.protocols())}")
    return 0


def cmd_prepare_data(args) -> int:
    site = load_site_config()
    raw_dir = Path(args.raw).expanduser() if args.raw else None
    try:
        if args.sample:
            data.prepare_sample(site, raw_dir=raw_dir)
        else:
            data.prepare(site, raw_dir=raw_dir, users=_split(args.users), reps=_split(args.reps),
                         stride=args.stride, max_points=args.max_points)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        sys.exit(f"prepare-data: {e}")
    return 0


def _run_matrix(function, reps, protocols, mode, *, alice, bob, carol, do_verify, dry_run) -> list[Result]:
    manifest = Manifest.load()
    site = load_site_config()
    reps = _resolve_reps(manifest, function, reps)
    protocols = protocols or [DEFAULT_PROTOCOL]
    pairs = list(_matrix(manifest, function, reps, protocols))

    if dry_run:
        print(f"Run matrix for {function} ({mode}):")
        for spec, proto in pairs:
            print(f"    {spec.representation:5s}  {proto}")
        return []

    results: list[Result] = []
    for spec, proto in pairs:
        label = f"{function}/{spec.representation}/{proto}/{mode}"
        print(f"==> {label}")
        try:
            prepared = data.write_inputs(spec, site, alice=alice, bob=bob, carol=carol)
        except FileNotFoundError as e:
            sys.exit(str(e))
        res = runner.execute(spec, proto, mode, site)
        if res.success and do_verify:
            ok, expected = verify.check(function, spec, prepared, res)
            res.verified, res.expected = ok, expected
        results.append(res)
        if not res.success:
            lines = res.error.strip().splitlines()
            print(f"    FAILED: {lines[-1] if lines else 'no RESULT parsed'}")
        else:
            v = "" if res.verified is None else ("  verified=OK" if res.verified else "  verified=MISMATCH")
            t = f"  time={res.mpc_time}s" if res.mpc_time is not None else ""
            print(f"    result={res.mpc_result}{t}{v}")
    return results


def cmd_run(args) -> int:
    mode = "distributed" if args.distributed else "local"
    results = _run_matrix(
        args.function, args.rep, _split(args.protocol), mode,
        alice=args.alice, bob=args.bob, carol=args.carol, do_verify=not args.no_verify, dry_run=args.dry_run,
    )
    if results:
        cls = Manifest.load().get(args.function).cls
        path = save(results, cls)
        print(f"\nSaved {len(results)} result(s) -> {path}")
    return 0


def cmd_verify(args) -> int:
    results = _run_matrix(
        args.function, args.rep, _split(args.protocol) or [DEFAULT_PROTOCOL], "local",
        alice=args.alice, bob=args.bob, carol=args.carol, do_verify=True, dry_run=False,
    )
    failures = [r for r in results if not r.success or r.verified is False]
    print()
    for r in results:
        status = "PASS" if (r.success and r.verified) else "FAIL"
        print(f"  [{status}] {r.representation:5s} {r.protocol}: "
              f"mpc={r.mpc_result} expected={r.expected}")
    if failures:
        print(f"\n{len(failures)} verification failure(s).")
        return 1
    print(f"\nAll {len(results)} verification(s) passed.")
    return 0


# --- parser ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="privela", description="Representation-aware MPC benchmarking.")
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("list", help="list functions, representations, protocols")
    lp.set_defaults(func=cmd_list)

    pp = sub.add_parser("prepare-data", help="build trajectory representations")
    pp.add_argument("--sample", action="store_true",
                    help=f"prepare only the sample users ({', '.join(data.SAMPLE_USERS)})")
    pp.add_argument("--raw", help="raw GPS directory (default: PRIVELA_DATA_DIR/raw)")
    pp.add_argument("--users", help="comma-separated user ids (default: all users found)")
    pp.add_argument("--reps", help="comma-separated representations: utm,h3[,road] "
                                   "(default utm,h3; road needs the roadprep extra)")
    pp.add_argument("--stride", type=int, help="keep every n-th GPS fix "
                                               f"(default {data.PLT_STRIDE} for .plt, 1 for .csv)")
    pp.add_argument("--max-points", type=int, default=data.MAX_TRACK_POINTS,
                    help=f"points kept per user (default {data.MAX_TRACK_POINTS})")
    pp.set_defaults(func=cmd_prepare_data)

    def add_run_args(sp):
        sp.add_argument("function")
        sp.add_argument("--rep", help="comma-separated representations, or 'all' (default)")
        sp.add_argument("--protocol", help=f"comma-separated protocols (default {DEFAULT_PROTOCOL})")
        sp.add_argument("--alice", default="000", help="user id for party 0 / reference")
        sp.add_argument("--bob", default="009", help="user id for party 1 / candidate A")
        sp.add_argument("--carol", default="011", help="user id for candidate B (3-arg fns)")

    rp = sub.add_parser("run", help="compile and run a function")
    add_run_args(rp)
    rp.add_argument("--distributed", action="store_true", help="run across the 3 party hosts")
    rp.add_argument("--no-verify", action="store_true", help="skip plaintext verification")
    rp.add_argument("--dry-run", action="store_true", help="print the run matrix and exit")
    rp.set_defaults(func=cmd_run)

    vp = sub.add_parser("verify", help="check MPC output against the plaintext reference")
    add_run_args(vp)
    vp.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
