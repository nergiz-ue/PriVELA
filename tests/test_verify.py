"""End-to-end check: the MPC output of every function equals its plaintext reference.

Runs on generated trajectories and the bundled road subgraph. Needs an MP-SPDZ installation
(``MP_SPDZ_PATH``); skipped otherwise.
"""

from __future__ import annotations

import pytest

from privela import data, runner, verify
from privela.config import DEFAULT_PROTOCOL, Manifest, RepSpec, SiteConfig, load_site_config

from .conftest import prepared_site

SPECS = [spec for fn in Manifest.load().functions.values() for spec in fn.reps.values()]

pytestmark = pytest.mark.skipif(
    not load_site_config().compile_py.exists(),
    reason=f"MP-SPDZ not found at {load_site_config().mp_spdz_path}",
)


@pytest.fixture(scope="module")
def mpc_site(tmp_path_factory: pytest.TempPathFactory) -> SiteConfig:
    return prepared_site(tmp_path_factory.mktemp("privela"))


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s.function}/{s.representation}")
def test_mpc_matches_plaintext(spec: RepSpec, mpc_site: SiteConfig) -> None:
    prepared = data.write_inputs(spec, mpc_site, alice="000", bob="009", carol="011")
    result = runner.execute(spec, DEFAULT_PROTOCOL, "local", mpc_site)
    assert result.success, result.error
    ok, expected = verify.check(spec.function, spec, prepared, result)
    assert ok, f"mpc={result.extra['results']} expected={expected}"
