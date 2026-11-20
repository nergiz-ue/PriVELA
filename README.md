# PriVELA: Privacy-Preserving Vehicular Location Analytics with Secure Multi-Party Computation

Nergiz Yuca, Nikolay Matyunin, Stefan Katzenbeisser — CANS 2026
([paper](https://doi.org/10.1007/978-981-92-7773-5_16))

PriVELA is a representation-aware MPC benchmarking framework for location-based functions.
It runs the same function under three trajectory representations — **UTM** planar coordinates,
the **H3** hexagonal grid, and **road network** graphs — and under several MPC protocols, locally
or distributed across machines, using [MP-SPDZ](https://github.com/data61/MP-SPDZ).

It isolates how the choice of spatial representation changes the dominant secure-computation
primitive (arithmetic vs. comparison vs. graph) and thus the cost of MPC.

## Install

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), and an MP-SPDZ installation
(tested with 0.4.0 and 0.4.2).

```bash
uv sync                         # core dependencies
uv sync --extra dev             # optional: pytest and ruff
uv sync --extra roadprep        # optional: osmnx + leuvenmapmatching (road data preparation)
cp .env.example .env            # then set MP_SPDZ_PATH and, for distributed runs, the hosts
```

## Data

PriVELA is evaluated on the [GeoLife GPS Trajectories](https://www.microsoft.com/en-us/download/details.aspx?id=52367)
dataset. Download it (about 300 MB) and prepare it:

```bash
mkdir -p data/raw
curl -L -o data/raw/geolife.zip "https://download.microsoft.com/download/F/4/8/F4894AA5-FDBC-481E-9285-D5F8C4C4F039/Geolife%20Trajectories%201.3.zip"
unzip -q data/raw/geolife.zip -d data/raw
uv run privela prepare-data --sample      # the four sample users only (enough for all functions)
uv run privela prepare-data               # or: every user in the dataset
```

`prepare-data` takes the first trajectory of each user (every 3rd fix, 15 points) from the
GeoLife layout (`Data/<user>/Trajectory/*.plt`) or from plain `<user>.csv` files (`lat,lon`
header) under `PRIVELA_DATA_DIR/raw` (or `--raw`). It writes UTM and H3 tracks to
`PRIVELA_PROCESSED_DIR` (default `PRIVELA_DATA_DIR/processed`), which the runs read. `--users`
selects specific users; `--sample` prepares users 000, 009, 010 and 011, the sample used for
verification.

The road representation ships with a small OpenStreetMap subgraph of Beijing
(`data/road/subgraph.json`). To rebuild it around your own users, with their trajectories
map-matched onto the road network, add `--reps utm,h3,road` (needs the `roadprep` extra and
network access); the first two users become the two parties.

## Quick start

```bash
uv run privela list                                              # functions × reps × protocols
uv run privela verify is_within_radius --rep utm,h3,road         # MPC result == plaintext
uv run privela run is_within_radius --rep utm --protocol semi2k  # local run
uv run privela run lcss --alice 000 --bob 009                    # choose the parties' users
uv run privela run is_within_radius --rep h3 --distributed       # multi-machine run
```

Results (runtime, communication, rounds) are stored under `results/` as JSON and CSV.

## Functions

| Class | Functions |
|-------|-----------|
| PTSP (point–point)         | `is_within_radius`, `is_closer_to_a_than_b`, `distance` |
| PTMP (point–set)           | `argmin_distance`, `count_within_radius`, `any_within_radius`, `topk_nearest` |
| PT (point–trajectory)      | `dwell_duration`, `entry_count`, `is_closer_to_traj_a_than_b` |
| TT (trajectory–trajectory) | `endpoints`, `trajectories_within_radius`, `count_in_zone`, `common_pois`, `count_close`, `lcss`, `jaccard` |

Every function is checked against a plaintext reference (`privela verify`). Supported protocols:
`semi2k` and `spdz2k` (two parties), `ring` and `sy-rep-ring` (three parties, honest majority).

## Code structure

- **`functions.yaml`** — manifest of every function: representations, MPC program, input layout,
  parameters, and skipped protocols.
- **`Programs/Source/<class>/<rep>_<class>_<fn>.mpc`** — MP-SPDZ programs, one per
  function × representation; shared circuits live in `Programs/Source/_helpers/`.
- **`privela/`** — data preparation, compilation, local and distributed execution, output
  parsing, result collection, and plaintext verification.
- **`tests/`** — `uv run pytest`, on generated trajectories; the end-to-end MPC tests are
  skipped without MP-SPDZ.

See [`docs/adding_a_function.md`](docs/adding_a_function.md) to add a function and
[`docs/distributed_setup.md`](docs/distributed_setup.md) for multi-machine runs.

## Publication

If you use PriVELA in your research, please cite our work:

```bibtex
@inproceedings{yuca2026privela,
  author    = {Nergiz Yuca and Nikolay Matyunin and Stefan Katzenbeisser},
  title     = {{PriVELA}: Privacy-Preserving Vehicular Location Analytics with Secure Multi-Party Computation},
  booktitle = {Cryptology and Network Security (CANS 2026)},
  publisher = {Springer Nature Singapore},
  year      = {2026},
  doi       = {10.1007/978-981-92-7773-5_16},
}
```

## License and data attribution

The code is released under the [MIT License](LICENSE).

- **GeoLife GPS Trajectories** (Microsoft Research Asia).
- `data/road/subgraph.json` is derived from **OpenStreetMap** data, © OpenStreetMap
  contributors, available under the [Open Database License](https://www.openstreetmap.org/copyright).
