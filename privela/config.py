"""Configuration: site settings (.env), the protocol table, and the function manifest.

"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is a hard dep, but degrade gracefully
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "functions.yaml"

# Load .env from the repo root once, on import. Real environment wins over the file.
load_dotenv(REPO_ROOT / ".env")


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


# --------------------------------------------------------------------------------------
# Protocol table — the fixed set of MPC protocols PriVELA benchmarks.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Protocol:
    name: str
    parties: int
    security: str          # "semi-honest" | "malicious"
    script: str            # MP-SPDZ Scripts/<script>
    binary: str            # party binary (used for distributed launch / stale-process cleanup)
    domain: str            # "arithmetic" | "binary"
    needs_ssl: bool = False  # honest-majority protocols require P*.pem certificates


PROTOCOLS: dict[str, Protocol] = {
    "semi2k":      Protocol("semi2k",      2, "semi-honest", "semi2k.sh",      "semi2k-party.x",             "arithmetic"),
    "ring":        Protocol("ring",        3, "semi-honest", "ring.sh",        "replicated-ring-party.x",    "arithmetic", needs_ssl=True),
    "sy-rep-ring": Protocol("sy-rep-ring", 3, "malicious",   "sy-rep-ring.sh", "sy-rep-ring-party.x",        "arithmetic", needs_ssl=True),
    "spdz2k":      Protocol("spdz2k",      2, "malicious",   "spdz2k.sh",      "spdz2k-party.x",             "arithmetic"),
    "semi-bin":    Protocol("semi-bin",    2, "semi-honest", "semi-bin.sh",    "semi-bin-party.x",           "binary"),
}

DEFAULT_PROTOCOL = os.environ.get("DEFAULT_PROTOCOL", "semi2k")


def get_protocol(name: str) -> Protocol:
    try:
        return PROTOCOLS[name]
    except KeyError:
        raise ValueError(f"Unknown protocol {name!r}. Known: {', '.join(PROTOCOLS)}")


# --------------------------------------------------------------------------------------
# Site / machine configuration (from .env or the environment).
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Host:
    ip: str
    user: str
    party: int


@dataclass(frozen=True)
class SiteConfig:
    mp_spdz_path: Path
    mp_spdz_remote: str            # MP_SPDZ_PATH as written, expanded by each party host's shell
    data_dir: Path
    processed_dir: Path
    hosts: list[Host]
    coordinator_ip: str
    port_base: int
    ssh_key: str
    remote_dir: str

    @property
    def compile_py(self) -> Path:
        return self.mp_spdz_path / "compile.py"

    def script(self, protocol: str) -> Path:
        return self.mp_spdz_path / "Scripts" / get_protocol(protocol).script


def _parse_hosts(raw: str) -> list[Host]:
    hosts: list[Host] = []
    for chunk in filter(None, (c.strip() for c in raw.split(","))):
        ip, user, party = chunk.split(":")
        hosts.append(Host(ip=ip, user=user, party=int(party)))
    return sorted(hosts, key=lambda h: h.party)


def load_site_config() -> SiteConfig:
    data_dir = _expand(os.environ.get("PRIVELA_DATA_DIR", str(REPO_ROOT / "data")))
    processed_env = os.environ.get("PRIVELA_PROCESSED_DIR", "").strip()
    processed_dir = _expand(processed_env) if processed_env else data_dir / "processed"
    mp_spdz = os.environ.get("MP_SPDZ_PATH", "~/mp-spdz")
    return SiteConfig(
        mp_spdz_path=_expand(mp_spdz),
        mp_spdz_remote=mp_spdz,
        data_dir=data_dir,
        processed_dir=processed_dir,
        hosts=_parse_hosts(os.environ.get("PRIVELA_HOSTS", "10.0.0.3:mpc:0,10.0.0.4:mpc:1,10.0.0.5:mpc:2")),
        coordinator_ip=os.environ.get("PRIVELA_COORDINATOR_IP", "10.0.0.5"),
        port_base=int(os.environ.get("PRIVELA_PORT_BASE", "14000")),
        ssh_key=os.environ.get("PRIVELA_SSH_KEY", "~/.ssh/privela_key"),
        remote_dir=os.environ.get("PRIVELA_REMOTE_DIR", "~/privela"),
    )


# --------------------------------------------------------------------------------------
# Function manifest (functions.yaml).
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RepSpec:
    """How one function is realised under one representation."""
    function: str
    representation: str
    cls: str                       # ptsp | ptmp | pt | tt
    program: str                   # MP-SPDZ program path, e.g. "ptsp/utm_ptsp_is_within_radius"
    inputs: str                    # named input layout handled by data.py (e.g. "two_points")
    params: dict[str, Any]         # merged function + per-rep parameters (radius, max_hops, ...)
    protocols_skip: tuple[str, ...]

    def protocols(self) -> list[str]:
        return [p for p in PROTOCOLS if p not in self.protocols_skip]


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    cls: str
    reps: dict[str, RepSpec] = field(default_factory=dict)


class Manifest:
    def __init__(self, functions: dict[str, FunctionSpec]):
        self.functions = functions

    @classmethod
    def load(cls, path: Path = MANIFEST_PATH) -> "Manifest":
        raw = yaml.safe_load(path.read_text()) or {}
        functions: dict[str, FunctionSpec] = {}
        for fname, fbody in raw.items():
            fclass = fbody["class"]
            fparams = dict(fbody.get("params", {}))
            reps: dict[str, RepSpec] = {}
            for rep, rbody in (fbody.get("reps") or {}).items():
                params = {**fparams, **dict(rbody.get("params", {}))}
                reps[rep] = RepSpec(
                    function=fname,
                    representation=rep,
                    cls=fclass,
                    program=rbody["program"],
                    inputs=rbody["inputs"],
                    params=params,
                    protocols_skip=tuple(rbody.get("protocols_skip", [])),
                )
            functions[fname] = FunctionSpec(name=fname, cls=fclass, reps=reps)
        return cls(functions)

    def get(self, function: str) -> FunctionSpec:
        try:
            return self.functions[function]
        except KeyError:
            raise ValueError(
                f"Unknown function {function!r}. Known: {', '.join(sorted(self.functions))}"
            )

    def resolve(self, function: str, representation: str) -> RepSpec:
        fn = self.get(function)
        try:
            return fn.reps[representation]
        except KeyError:
            raise ValueError(
                f"Function {function!r} has no {representation!r} representation "
                f"(available: {', '.join(sorted(fn.reps))})"
            )
