from __future__ import annotations

from privela.runner import parse_output

OUTPUT = """\
RESULT 1
RESULT -3
The following benchmarks are including preprocessing (offline phase).
Time = 0.0421 seconds
Time1 = 0.0312 seconds (1.2 MB, 12 rounds)
Data sent = 1.37 MB in ~58 rounds (party 0 only)
Global data sent = 2.74 MB (all parties)
"""


def test_parse_output() -> None:
    parsed = parse_output(OUTPUT)
    assert parsed.results == [1, -3]
    assert parsed.result == 1
    assert parsed.mpc_time == 0.0421
    assert parsed.communication_mb == 1.37
    assert parsed.rounds == 12


def test_parse_output_without_result() -> None:
    parsed = parse_output("Fatal error: something went wrong")
    assert parsed.results == []
    assert parsed.result is None


def test_remote_launch_uses_unexpanded_mp_spdz_path(monkeypatch) -> None:
    from dataclasses import replace

    from privela import runner
    from privela.config import Host, load_site_config

    calls: list[list[str]] = []
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **_: calls.append(cmd))
    site = replace(load_site_config(), mp_spdz_remote="~/mp-spdz", remote_dir="~/privela",
                   port_base=14000)
    runner._launch_party(site, Host("10.0.0.4", "mpc", 1), "utm_ptsp_distance", 2, "10.0.0.3",
                         "semi2k-party.x")
    assert calls[0][-2] == "mpc@10.0.0.4"
    assert calls[0][-1] == ("cd ~/privela && LD_LIBRARY_PATH=~/mp-spdz:$LD_LIBRARY_PATH "
                            "~/mp-spdz/semi2k-party.x 1 utm_ptsp_distance -pn 14000 -h 10.0.0.3 -v -N 2")
