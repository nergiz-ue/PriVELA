# Adding a function

A new function can be added by writing one MP-SPDZ program per representation, a
manifest entry, and a plaintext reference.

### 1. Write an MP-SPDZ program per representation
- store `.mpc` program at `Programs/Source/<class>/<rep>_<class>_<function>.mpc`.
- read public values with `public_input()`, private inputs with `sint.get_input_from(party)`;
- wrap the secure computation block in `start_timer(1) ... stop_timer(1)`;
- emit the cleartext output as the **last** line: `print_ln("RESULT %s", value.reveal())`
  (the runner parses `RESULT <int>`).

See existing functions as reference examples, e.g., `Programs/Source/ptsp/{utm,h3,road}_ptsp_is_within_radius.mpc`.

### 2. Add a manifest entry (`functions.yaml`)

```yaml
my_function:
  class: ptsp                 # ptsp | ptmp | pt | tt
  params: {radius_m: 500}     # function-wide params
  reps:
    utm:
      program: ptsp/utm_ptsp_my_function
      inputs: two_points       # input layout handled by privela/data.py
      protocols_skip: [semi-bin]
    # h3:, road: ...
```

### 3. Input layout (only if new)

If your inputs match an existing layout (`two_points`, `ring_and_cell`, `two_nodes`), reuse
it. Otherwise add a writer in `privela/data.py` (`write_inputs` dispatch) that reads the
prepared tracks (`load_track`) or road subgraph (`load_road_graph`), writes
`Player-Data/Input-P<party>-0` and, if needed, `Programs/Public-Input/<program-basename>`,
and returns a `Prepared` carrying the plaintext inputs for verification.

### 4. Plaintext reference (`privela/verify.py`)

Register a plaintext implementation per input layout:

```python
@register("my_function", "two_points")
def _mine(spec, p):
    ...
    return int(result)
```

### 5. Verify

```bash
privela verify my_function --rep utm,h3,road
```

`verify` runs the MPC kernel on the prepared data (see the main README) and asserts
equality with the plaintext reference. `uv run pytest` picks the new function up from the
manifest automatically: `tests/test_manifest.py` checks its inputs and plaintext reference, and
`tests/test_verify.py` runs it end to end when MP-SPDZ is installed.
