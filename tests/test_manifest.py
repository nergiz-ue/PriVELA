from __future__ import annotations

import pytest

from privela import data, verify
from privela.config import PROTOCOLS, REPO_ROOT, Manifest, RepSpec

MANIFEST = Manifest.load()
SPECS = [spec for fn in MANIFEST.functions.values() for spec in fn.reps.values()]


def _id(spec: RepSpec) -> str:
    return f"{spec.function}/{spec.representation}"


@pytest.mark.parametrize("spec", SPECS, ids=_id)
def test_program_exists(spec: RepSpec) -> None:
    assert (REPO_ROOT / "Programs" / "Source" / f"{spec.program}.mpc").is_file()
    assert set(spec.protocols_skip) <= set(PROTOCOLS)


@pytest.mark.parametrize("spec", SPECS, ids=_id)
def test_inputs_and_plaintext_reference(spec: RepSpec, site) -> None:
    prepared = data.write_inputs(spec, site, alice="000", bob="009", carol="011")
    assert prepared.inputs == spec.inputs
    assert (site_inputs := list((data.REPO_ROOT / "Player-Data").glob("Input-P*-0")))
    assert all(f.read_text().split() == [str(int(v)) for v in f.read_text().split()]
               for f in site_inputs)
    expected = verify.plaintext_result(spec.function, spec, prepared)
    values = expected if isinstance(expected, (list, tuple)) else [expected]
    assert all(isinstance(v, int) for v in values)
