# Siglent SDS1202X-E bench tooling

Control and read a Siglent SDS1202X-E oscilloscope over LAN (SCPI on TCP
port 5025). Two entry points share one `Scope` class:

- **`scope.py`** — library, CLI, and live dashboards.
- **`scope_mcp.py`** — MCP server exposing the same capabilities as tools.

Default instrument IP is `192.168.1.139` (override with `--ip` or the
`SCOPE_IP` environment variable). The scope allows **one client at a time**,
so don't drive it from the CLI and the MCP server simultaneously.

## Setup

Uses the `~/PY3` virtualenv. Dependencies: `pyvisa` + `pyvisa-py` (LAN
SCPI), `numpy`, `rich` (terminal dashboard), `matplotlib` (GUI / plots),
`mcp` (server). `ffmpeg` is needed for PNG screenshots.

```bash
source ~/PY3/bin/activate
python3 scope.py idn        # sanity check: prints *IDN?
```

## 1. Standalone — dashboard / CLI

| Verb | Purpose |
|---|---|
| `idn` | identify the instrument (`*IDN?`) |
| `meas` | PAVA measurements (MEAN/PKPK/FREQ/DUTY/MAX/MIN/…) |
| `grab` | raw sample capture of a channel → numpy / npz / png |
| `math` | MATH trace readback (non-FFT) |
| `shot` | screenshot the 800x480 display |
| `setup` | channel verticals + timebase (VDIV/OFST/CPL/ATTN/BWL/TDIV/TRDL) |
| `trig` | edge-trigger setup (TRSE/TRSL/TRLV/SET50/TRMD) |
| `acq` | acquisition mode (sampling/peak/average/hires) |
| `single` | arm + wait-for-trigger + capture one frame |
| `save` / `recall` | store/restore the full setup to internal slots 1–20 |
| `aset` | front-panel Auto setup |
| `split` | stack two 0–5 V signals in separate screen halves |
| `watch` | poll a measurement until a condition holds (optional spoken alert) |
| `q` / `c` | raw SCPI query / command(s) |
| `dash` / `gui` | live terminal / matplotlib displays |

```bash
# --- Live displays ---
python3 scope.py gui                     # matplotlib window, real traces (CH1 yellow, CH2 magenta)
python3 scope.py gui --span-ms 3 --points 3000
python3 scope.py dash                    # terminal sparkline dashboard, Ctrl-C quits

# --- One-shot reads ---
python3 scope.py meas C1 all             # MEAN PKPK FREQ DUTY MAX MIN
python3 scope.py meas C2 FREQ DUTY
python3 scope.py shot /tmp/screen.png    # screenshot the 800x480 display (needs ffmpeg)
python3 scope.py grab C1 --span-ms 3 --out /tmp/c1.npz --plot /tmp/c1.png   # raw samples
python3 scope.py math --def 'C1-C2' --out /tmp/math.npz --plot /tmp/math.png   # MATH trace (non-FFT)

# --- Configure → arm → capture → snapshot (the unattended loop) ---
python3 scope.py setup C1 --vdiv 1 --ofst -2.5 --cpl D1M --attn 10 --tdiv 200US
python3 scope.py acq sampling                  # or peak/average/hires (set while running)
python3 scope.py trig C1 --slope pos --set50 --mode norm   # or --level 2.5
python3 scope.py single C1 --timeout 30 --out /tmp/shot.npz --say "captured"
python3 scope.py save 5                        # store this full setup to slot 5
python3 scope.py recall 5                      # restore it later

# As a one-liner:
python3 scope.py setup C1 --vdiv 1 --tdiv 200US && python3 scope.py trig C1 --slope pos --set50 \
  && python3 scope.py acq sampling && python3 scope.py single C1 --out run.npz && python3 scope.py save 5

# --- Control / raw passthrough ---
python3 scope.py aset                    # front-panel Auto
python3 scope.py split C1 C2             # stack two 0-5 V signals, no overlap
python3 scope.py q "C1:VDIV?"            # raw SCPI query
python3 scope.py c "TDIV 200US" "C1:TRA ON"   # raw SCPI commands
python3 scope.py watch C1 PKPK ">3" --timeout 120 --say "contact"   # wait for a condition
```

**Acquisition-mode caveat:** `acq` is only honored while the scope is acquiring,
and `average`/`hires` need continuous/multiple acquisitions — so set the mode
*before* arming, and use `sampling` or `peak` for single-shot captures. `acq`
reports `applied: False` if the mode didn't take.

As a library:

```python
from scope import Scope
with Scope() as s:
    print(s.meas(1, "FREQ"))
    t, v = s.grab(1, span_s=0.003)   # numpy arrays
```

## 2. As an MCP server

`scope_mcp.py` exposes the capabilities as MCP tools over stdio. Register
with Claude Code (or any MCP client):

```bash
claude mcp add scope -- ~/PY3/bin/python /path/to/siglent/scope_mcp.py
# point at a different instrument:
claude mcp add scope --env SCOPE_IP=192.168.1.50 -- ~/PY3/bin/python /path/to/siglent/scope_mcp.py
```

Tools: `scope_idn`, `scope_measure`, `scope_query`, `scope_command`,
`scope_autoset`, `scope_status`, `scope_grab`, `scope_math`,
`scope_acquire`, `scope_setup`, `scope_trigger`, `scope_single`,
`scope_capture`, `scope_save`, `scope_recall`, `scope_screenshot`,
`scope_split`, `scope_watch`. Each opens a fresh short-lived socket and closes it before
returning, so MCP calls and a hand-run `scope.py` can interleave. On connect
each call drains any bytes a previously aborted transfer left in the link, so
one desynced read can't poison later calls.

`scope_grab`, `scope_single`, and `scope_math` take `plot=True` to also return
a rendered PNG of the captured trace (as an MCP image), so the model can see
the waveform shape rather than only the summary scalars.

The full instrument state (IDN, timebase, trigger, acquisition mode, per-channel
vertical config) is available two ways for ambient context before configuring or
capturing: the read-only resource `scope://status` and the `scope_status` tool
(same content, for clients that don't surface resources).

`scope_capture` collapses the whole configure → arm → capture loop into one call
(channel/timebase setup + optional acquisition mode + edge trigger + single-shot),
saving the per-step round-trips when an agent just wants "set this up and grab it".

## Gotchas

- Single-client instrument: a running `gui`/`dash` polls continuously and
  will block other clients while open.
- `gui` and `shot` need a display; `shot`/screenshots need `ffmpeg`.
- `FREQ` and `DUTY` need a stable trigger to be meaningful.

Verified SCPI recipes and quirks: see `scope-remote.org`. Vendor manual:
`docs/SDS_ProgrammingGuide_EN02E.pdf`.
