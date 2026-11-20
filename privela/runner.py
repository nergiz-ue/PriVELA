"""Compile and run MP-SPDZ programs locally or distributed.

One implementation of each path. The distributed executor is the 4-phase
prepare -> sync -> ssh-execute -> parse pattern, driven entirely by .env host config.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT, RepSpec, SiteConfig, get_protocol
from .results import Result

HELPERS_DIR = REPO_ROOT / "Programs" / "Source" / "_helpers"
COMPILE_TIMEOUT = 600
RUN_TIMEOUT = 1800


# --- Output parsing -------------------------------------------------------------------
@dataclass
class Parsed:
    results: list[int] = field(default_factory=list)   # all RESULT values, in order
    mpc_time: float | None = None
    online_time: float | None = None
    offline_time: float | None = None
    communication_mb: float | None = None
    rounds: int | None = None

    @property
    def result(self) -> int | None:
        return self.results[0] if self.results else None


_RESULT_RE = re.compile(r"RESULT\s+(-?\d+)")
_RE = {
    "mpc_time": re.compile(r"Time\s*=\s*([\d.]+)\s*seconds"),
    "online_time": re.compile(r"([\d.]+)\s*seconds.*?on the online phase", re.I),
    "offline_time": re.compile(r"([\d.]+)\s*seconds.*?on the (?:offline|preprocessing) phase", re.I),
    "communication_mb": re.compile(r"(?:Data sent|Global data sent)\s*=\s*([\d.]+)\s*MB"),
    "rounds": re.compile(r"(\d+)\s*rounds", re.I),
}


def parse_output(text: str) -> Parsed:
    p = Parsed(results=[int(m) for m in _RESULT_RE.findall(text)])
    for name, rx in _RE.items():
        m = rx.search(text)
        if m:
            setattr(p, name, int(m.group(1)) if name == "rounds" else float(m.group(1)))
    return p


# --- Compilation ----------------------------------------------------------------------
def compile_program(spec: RepSpec, protocol: str, site: SiteConfig) -> tuple[bool, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HELPERS_DIR), env.get("PYTHONPATH", "")]))
    cmd = [sys.executable, str(site.compile_py), "-Y", "-E", protocol, spec.program]
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True,
                              text=True, timeout=COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"compile timeout ({COMPILE_TIMEOUT}s)"
    if proc.returncode != 0:
        return False, f"compile failed:\n{proc.stdout}\n{proc.stderr}"
    return True, proc.stdout


# --- Local execution ------------------------------------------------------------------
def ensure_ssl(site: SiteConfig, nparties: int) -> None:
    """Generate MP-SPDZ SSL certificates (Player-Data/P*.pem) if missing.

    Honest-majority protocols (ring, sy-rep-ring) require them; setup-ssl.sh writes into the
    Player-Data of the current working directory.
    """
    pd = REPO_ROOT / "Player-Data"
    if all((pd / f"P{i}.pem").exists() for i in range(nparties)):
        return
    setup = site.mp_spdz_path / "Scripts" / "setup-ssl.sh"
    subprocess.run([str(setup), str(nparties)], cwd=REPO_ROOT,
                   capture_output=True, text=True, timeout=120, check=True)


def run_local(spec: RepSpec, protocol: str, site: SiteConfig) -> tuple[str, str]:
    proto = get_protocol(protocol)
    if proto.needs_ssl:
        ensure_ssl(site, proto.parties)
    cmd = [str(site.script(protocol)), spec.program, "-v"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    return proc.stdout, proc.stderr


# --- Distributed execution (3 machines) -----------------------------------------------
def _ssh_base(site: SiteConfig) -> list[str]:
    return [
        "ssh", "-i", os.path.expanduser(site.ssh_key),
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ]


def _is_local_checkout(remote_dir: str) -> bool:
    return Path(os.path.expanduser(remote_dir)).resolve() == REPO_ROOT.resolve()


def _sync_to_hosts(site: SiteConfig, parties: list) -> None:
    subdirs = ["Programs/Bytecode/", "Programs/Schedules/", "Programs/Public-Input/", "Player-Data/"]
    ssh = " ".join(_ssh_base(site))
    for host in parties:
        if host.ip == site.coordinator_ip and _is_local_checkout(site.remote_dir):
            continue  # this party runs from the coordinator's own checkout
        dest_base = f"{host.user}@{host.ip}:{site.remote_dir}/"
        for sub in subdirs:
            src = REPO_ROOT / sub
            if not src.exists():
                continue
            subprocess.run(
                ["rsync", "-az", "-e", ssh, "--mkpath", f"{src}/", f"{dest_base}{sub}"],
                check=True, timeout=120, capture_output=True, text=True,
            )


def _launch_party(site: SiteConfig, host, program: str, nparties: int | None,
                  party0_ip: str, binary: str) -> subprocess.CompletedProcess:
    remote_mp = site.mp_spdz_remote
    pn = site.port_base
    args = [f"{remote_mp}/{binary}", str(host.party), program, "-pn", str(pn), "-h", party0_ip, "-v"]
    if nparties is not None:
        args += ["-N", str(nparties)]
    remote = (
        f"cd {site.remote_dir} && "
        f"LD_LIBRARY_PATH={remote_mp}:$LD_LIBRARY_PATH " + " ".join(args)
    )
    cmd = _ssh_base(site) + [f"{host.user}@{host.ip}", remote]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT)


def run_distributed(spec: RepSpec, protocol: str, site: SiteConfig) -> tuple[str, str]:
    proto = get_protocol(protocol)
    parties = site.hosts[: proto.parties]
    if len(parties) < proto.parties:
        raise RuntimeError(f"{protocol} needs {proto.parties} hosts; PRIVELA_HOSTS has {len(site.hosts)}")

    # Honest-majority protocols need SSL certs; generate them locally so the Player-Data sync
    # distributes the same set to every party host.
    if proto.needs_ssl:
        ensure_ssl(site, proto.parties)
    _sync_to_hosts(site, parties)

    # 2-party protocols need an explicit -N; fixed-arity (replicated 3pc) protocols omit it.
    nparties = proto.parties if proto.parties == 2 else None
    party0_ip = parties[0].ip
    # MP-SPDZ compiles "<group>/<name>" to a basename schedule/bytecode; the party binary is
    # launched with the basename (the local Scripts/*.sh wrapper does the same).
    program = spec.program.split("/")[-1]

    outputs: dict[int, subprocess.CompletedProcess] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(parties)) as ex:
        futs = {
            ex.submit(_launch_party, site, h, program, nparties, party0_ip, proto.binary): h
            for h in parties
        }
        for fut in concurrent.futures.as_completed(futs):
            host = futs[fut]
            outputs[host.party] = fut.result()

    stdout = outputs[0].stdout if 0 in outputs else ""
    stderr = "\n".join(f"[party {p}]\n{cp.stderr}" for p, cp in sorted(outputs.items()))
    return stdout, stderr


# --- High-level orchestration ---------------------------------------------------------
def execute(spec: RepSpec, protocol: str, mode: str, site: SiteConfig) -> Result:
    """Compile + run one (function, rep, protocol) and return a Result (without verification)."""
    res = Result(function=spec.function, representation=spec.representation,
                 protocol=protocol, mode=mode)
    if not site.compile_py.exists():
        res.error = (f"MP-SPDZ not found at {site.mp_spdz_path} "
                     "(set MP_SPDZ_PATH in .env to the directory containing compile.py)")
        return res
    ok, msg = compile_program(spec, protocol, site)
    if not ok:
        res.error = msg
        return res
    try:
        stdout, stderr = (run_distributed if mode == "distributed" else run_local)(spec, protocol, site)
    except Exception as e:  # noqa: BLE001 - surface any run failure as a result row
        res.error = f"{type(e).__name__}: {e}"
        return res

    parsed = parse_output(stdout + "\n" + stderr)
    res.mpc_result = parsed.result
    res.extra["results"] = parsed.results
    res.mpc_time = parsed.mpc_time
    res.online_time = parsed.online_time
    res.offline_time = parsed.offline_time
    res.communication_mb = parsed.communication_mb
    res.rounds = parsed.rounds
    res.success = parsed.result is not None
    if not res.success:
        res.error = (stderr or stdout)[-2000:]
    return res
