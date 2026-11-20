# Distributed setup (3 machines)

PriVELA runs MPC either locally (all parties as processes on one host) or distributed across
two or three machines.

Distributed runs are executed by `privela/runner.py` in four steps:
**prepare** (compile + write inputs on the coordinator) -> **sync** (rsync program bytecode,
schedules, public + player data to each party host) -> **execute** (launch each party binary
over SSH) -> **parse** (collect party 0's output).

## Overview

Hostnames are configured in `.env` (`PRIVELA_HOSTS = ip:user:party,...`), for example:

| Role | Host | Internal IP | Party |
|------|------|-------------|-------|
| Party 0            | p0 | 10.0.0.3 | 0 |
| Party 1            | p1 | 10.0.0.4 | 1 |
| Party 2            | p2 | 10.0.0.5 | 2 |

The coordinator (the machine you run `privela` on) compiles, prepares inputs, distributes
them, and launches the party binaries over SSH. All hosts are assumed to share an identical clone of the repo at `PRIVELA_REMOTE_DIR` and the same MP-SPDZ install at `MP_SPDZ_PATH` (a leading `~` is expanded on each host, so user names may differ).

The coordinator can be one of the parties (e.g., p0) or a separate machine with network access to all parties. For 2-party protocols only p0 and p1 are used.

## One-time setup

Run from the coordinator in the repo root

```bash
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
HOSTS=$(get PRIVELA_HOSTS)
KEY=$(get PRIVELA_SSH_KEY);  KEY="${KEY/#\~/$HOME}"
MPS=$(get MP_SPDZ_PATH);     MPS="${MPS/#\~/$HOME}"
REMOTE=$(get PRIVELA_REMOTE_DIR)
PORT=$(get PRIVELA_PORT_BASE)
```

1. **Passwordless SSH access.** The coordinator must be able to SSH into each party host with `PRIVELA_SSH_KEY` *without a password*.  The **public** key must already be in each party's `~/.ssh/authorized_keys`. Host keys are accepted on first connection (`StrictHostKeyChecking=accept-new`), so run the parties on a network you trust or add them to `known_hosts` beforehand.

2. **SSL certificates** *(optional)*. Honest-majority protocols
   (`ring`, `sy-rep-ring`) require SSL certificates that are generated within MP-SPDZ using `Scripts/setup-ssl.sh` script. `privela` distributed
   honest-majority runs generate `Player-Data/P*.pem` on the coordinator if certificates are missing and rsyncs the same set to all party hosts.
   
   Run this manually to **recreate certs**:
   ```bash
   nparties=$(awk -F, '{print NF}' <<<"$HOSTS")
   rm -f Player-Data/P*.pem                          # only if forcing a refresh
   "$MPS"/Scripts/setup-ssl.sh "$nparties"           # writes Player-Data/P0..P$((nparties-1)).pem
   for h in ${HOSTS//,/ }; do
     IFS=: read ip user party <<<"$h"
     rsync -e "ssh -i $KEY" -a Player-Data/P*.pem "$user@$ip:$REMOTE/Player-Data/"
   done
   ```

3. **Firewall** — Ensure that the necessary MP-SPDZ ports are open on each party (e.g., using ufw).

## Running

```bash
privela run is_within_radius --rep utm --protocol semi2k --distributed
```

- `semi2k`/`spdz2k` are 2-party: only the first two hosts are used (party 0 acts as server).
- `ring`/`sy-rep-ring` are 3-party and need the SSL certs above.
- Inputs are prepared on the coordinator and `Player-Data` is copied to every party host, so
  all hosts see all parties' inputs. This is a benchmarking setup, not a deployment.
- A run that times out can leave party processes behind on the hosts; stop them before the
  next run.
- **The programs always run sequentially**, as they share `Player-Data` inputs and may contaminate timings.
