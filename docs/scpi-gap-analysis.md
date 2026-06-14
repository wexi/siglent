# SDS1202X-E SCPI feature gap analysis

Deep-research pass (2026-06-13) on what the SDS1202X-E exposes over SCPI vs.
what `scope.py` / `scope_mcp.py` currently wrap. 23/25 verified claims against
the official Siglent guides.

## Reference set
The X-E uses the **legacy** command set in the **EN02 / PG01-E02D** programming
guide (`TRMD`, `WFSU`, `C1:VDIV`, no colon-prefixed SCPI). The newer **EN11D**
guide (Feb 2023) does **not** cover the X-E — ignore it.

- EN02E: https://int.siglent.com/u_file/download/25_11_25/SDS1000%20Series&SDS2000X&SDS2000X-E_ProgrammingGuide_EN02E.pdf
- PG01-E02D: https://int.siglent.com/u_file/document/SDS1000%20Series&SDS2000X&SDS2000X-E_ProgrammingGuide_PG01-E02D.pdf
- 2017 guide: https://siglentna.com/wp-content/uploads/dlm_uploads/2017/10/ProgrammingGuide_forSDS-1-1.pdf
- X-E manual (ManualsLib): https://www.manualslib.com/manual/3501239/Siglent-Sds1202x-E.html

## Already correct in the tool (confirmed)
- `CHDR OFF` in `Scope.__init__` — strips headers/units from replies.
- `grab` uses byte-count `read_bytes`, not a `\n` terminator — waveform binary
  contains `0x0A`/`0x0D`; a term-char read would truncate it.
- `np.int8` decode — correct (subtract 256). Older guide's "minus 255" is a
  documented off-by-one typo.
- Reads `SARA?`/`VDIV?`/`OFST?` dynamically — adapts to the dual-channel limit
  (500 MSa/s, 7 Mpts/ch with both channels on; 1 GSa/s, 14 Mpts single).

## Implementation status

**Tier-1 and Tier-2 MATH are DONE** (built and hardware-verified, 2026-06-13/14).
The tool now does the full unattended `setup → acq → trig → single → save` loop
plus MATH readback. Hardware-found firmware quirks live in `scope.py` docstrings
and the auto-memory `scpi-feature-roadmap`.

### Tier 1 — first-class verbs + MCP tools — DONE
| Capability | Verb / tool | Commands | Notes (verified on fw 1.3.28) |
|---|---|---|---|
| Trigger setup | `trig` / `scope_trigger` | `TRSE EDGE/…`, `C<n>:TRSL`, `C<n>:TRLV`, `SET50`, `TRMD AUTO\|NORM\|SINGLE\|STOP` | `SET50` is a no-op under dual-level triggers (Runt). `TRLV` clamped to ±4.5 div * V/div. `TRMD?` is NOT a reliable run-state signal. |
| Channel/timebase setup | `setup` / `scope_setup` | `C<n>:VDIV/OFST/CPL/ATTN`, `BWL C<n>,ON`, `TDIV`, `TRDL` | BWL needs the **legacy global form** `BWL C1,ON` — `C1:BWL ON` is silently ignored. ATTN applied **before** VDIV/OFST (probe factor rescales the probe-referred scale). |
| Single-shot acquire + status | `single` / `scope_single` | `TRMD SINGLE`, poll `INR?` (bit 0 = new acquisition, bit 13 = armed), then `WF?` | Arm → wait-for-trigger → grab, unattended. `TRMD SINGLE` resets bit 0, so arm-then-poll (no pre-clear). Settle before grab — an unready frame's `WF?` reply lacks the `#` header and desyncs the socket. |
| Acquisition mode | `acq` / `scope_acquire` | `ACQW SAMPLING\|PEAK_DETECT\|AVERAGE,<n>\|HIGH_RES` | **ACQW is only honored while acquiring** — a STOPped scope silently ignores it. AVERAGE/HIGH_RES are incompatible with single-shot. `acq` returns `applied:False` on mismatch. |
| Save/recall setup | `save`/`recall` / `scope_save`/`scope_recall` | `*SAV`/`*RCL` (slots 1–20) | Full-setup snapshot. Round-trip verified (save → change → recall restores config). `STPN`/`RCPN` USB XML files not wrapped. |

### Tier 2 — MATH readback DONE; REF not WF?-readable
- **MATH-trace readback — DONE** (`math` / `scope_math`, via shared `_read_trace`).
  Non-FFT only; the FFT trace is *not* retrievable over `WF?` (FFT is 8-bit-limited
  ~49 dB, single channel, no phase, fixed ~5 windows). Scaling verified: MATH uses
  25 codes/div like analog — `volts = code*(MTVD/25) - (MTVP/50)*MTVD` (MTVP in
  screen pixels, 50 px = 1 div).
- **Reference waveforms: NOT readable over `WF?`.** The `WF?` trace list is
  `{C1–C4, MATH, D0–D15}` — REF is absent. REF stays control-only via raw `c`
  (`REFSR/REFLA/REFDS/REFSA/REFSC/REFPO/REFCL`), and `REFSC?`/`REFPO?` readbacks
  are garbage on this firmware.

### Tier 3 — leave to raw `q`/`c` passthrough (not built)
Serial-decode trigger config (`TRSPI:MISO …` confirmed), cursors, measurement
statistics. Deep protocol-specific trees not worth wrapping.

## WFSU best practice
`FP` first point, `SP` sparsing (0 or 1 = every point), `NP` points (0 = all);
SPO `TYPE` 0=screen / 1=full memory. Power-on defaults are **not** reliable
(refuted in verification) — set `WFSU SP,..,NP,..,FP,0` explicitly before a grab.

## Open questions
- Can **decoded** serial frames (vs. decode *trigger* config) be read back over
  the legacy command set? Only SPI trigger config verified — likely not.
- Cursors / measurement statistics / history (sequence) acquisition over SCPI on
  this specific model — named in the question but unverified.
- Practical max transfer time for a full 14 Mpts `DAT2` read over LAN; does
  `WFSU TYPE,1` reliably work on the 1202X-E specifically.
