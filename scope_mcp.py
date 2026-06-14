#!/usr/bin/env python3
"""MCP server exposing the Siglent SDS1202X-E as callable tools.

Wraps the Scope class from scope.py. Each tool opens a fresh socket and
closes it before returning: the instrument allows only one client at a
time, so a persistent connection would collide with hand-run scope.py.

Run (stdio, for Claude Desktop / Claude Code):
    ~/PY3/bin/python scope_mcp.py

Register with Claude Code:
    claude mcp add scope -- ~/PY3/bin/python /path/to/siglent/scope_mcp.py

Override the instrument address with SCOPE_IP (default from scope.py).
"""

import json
import os
import tempfile
import time

from mcp.server.fastmcp import FastMCP, Image

from scope import DEFAULT_IP, NoWaveform, Scope, parse_condition

SCOPE_IP = os.environ.get("SCOPE_IP", DEFAULT_IP)
MEAS_ITEMS = ["MEAN", "PKPK", "FREQ", "DUTY", "MAX", "MIN", "RMS", "PER"]

mcp = FastMCP("siglent-scope")


def _scope():
    """Fresh short-lived connection, drained of any stale bytes a prior
    aborted transfer may have left in the link. Use as a context manager."""
    s = Scope(SCOPE_IP)
    s.drain()
    return s


def _diagnose_no_trigger(s, channel, slope):
    """After a single-shot times out, free-run briefly and describe what the
    input actually looks like relative to the configured trigger, so a flat
    or absent signal is distinguishable from a misconfigured trigger.

    A timeout alone can't tell 'board is dead / probe off' from 'signal is
    there but the level/slope is wrong'. This samples the input in AUTO and
    compares its range against the effective trigger level."""
    info = {"slope": slope}
    try:
        level = s.num(f"C{channel}:TRLV?")
        info["trigger_level"] = level
        info["acq"] = s.acq_state()["mode"]
        s.cmd("TRMD AUTO")
        time.sleep(0.3)
        t, v = s.grab(channel, 0.003, 20000)
        vmin, vmax = float(v.min()), float(v.max())
        vpp = vmax - vmin
        info["signal"] = {"vmin": round(vmin, 3), "vmax": round(vmax, 3),
                          "vpp": round(vpp, 3)}
        flat = vpp < 0.3
        crosses = level is not None and vmin < level < vmax
        if flat:
            info["diagnosis"] = (
                f"input is essentially flat at {(vmin + vmax) / 2:.2f} V "
                f"(vpp {vpp:.2f}) — nothing for an edge trigger to fire on. "
                f"Check that the source is actually switching and the probe "
                f"is on the right node.")
        elif level is not None and not crosses:
            info["diagnosis"] = (
                f"signal spans {vmin:.2f}..{vmax:.2f} V but the trigger level "
                f"is {level} V — outside the signal, so no {slope}-edge can "
                f"cross it. Move the level inside the range (or use set50).")
        else:
            info["diagnosis"] = (
                f"signal is present ({vmin:.2f}..{vmax:.2f} V) and straddles "
                f"the {level} V level — suspect wrong slope/source, or an "
                f"acq mode (AVERAGE/HIGH_RES) that won't survive arming.")
    except NoWaveform:
        info["diagnosis"] = ("could not sample the input even in AUTO — the "
                             "channel may be off or disconnected.")
    except Exception as e:                       # diagnosis is best-effort
        info["diagnosis"] = f"could not characterize the input: {e}"
    return info


def _plot_trace(t, v, title):
    """Render a captured trace to a PNG and return it as an MCP Image so the
    model can see the waveform shape, not just summary scalars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    path = os.path.join(tempfile.gettempdir(), "scope_mcp_trace.png")
    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.plot(t * 1000, v, lw=0.8)
    ax.set_xlabel("ms")
    ax.set_ylabel("V")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return Image(path=path)


@mcp.resource("scope://status", mime_type="application/json",
              description="Live snapshot of the scope's full state — IDN, "
              "timebase (tdiv/trdl/sample-rate), trigger (select/mode/source/"
              "slope/level), acquisition mode, and per-channel vertical config "
              "(on/vdiv/ofst/cpl/attn/bwl). Read this for ambient context "
              "before configuring or capturing; queries only, never disturbs "
              "the scope.")
def scope_status_resource() -> str:
    with _scope() as s:
        return json.dumps(s.status(), indent=2)


@mcp.tool()
def scope_status() -> dict:
    """Full instrument-state snapshot — IDN, timebase, trigger, acquisition
    mode, and per-channel vertical config. Same content as the scope://status
    resource, as a tool for clients that don't surface resources. Queries
    only, never disturbs the scope; read it for ambient context before
    configuring or capturing.
    """
    with _scope() as s:
        return s.status()


@mcp.tool()
def scope_idn() -> str:
    """Identify the instrument (vendor, model, serial, firmware)."""
    with _scope() as s:
        return s.query("*IDN?")


@mcp.tool()
def scope_measure(channel: int = 1, items: list[str] | None = None) -> dict:
    """Read PAVA measurements on a channel.

    items: any of MEAN, PKPK, FREQ, DUTY, MAX, MIN, RMS, PER (default: all).
    Returns {item: value-or-None}; None means the scope read '****'
    (unmeasurable). FREQ needs a stable trigger and is advisory.
    """
    items = [i.upper() for i in (items or MEAS_ITEMS)]
    with _scope() as s:
        return {item: s.meas(channel, item) for item in items}


@mcp.tool()
def scope_query(query: str) -> str:
    """Send one raw SCPI query and return the reply (headers already off)."""
    with _scope() as s:
        return s.query(query)


@mcp.tool()
def scope_command(commands: list[str]) -> str:
    """Send one or more raw SCPI commands that expect no reply.

    Examples: ["TDIV 200US", "C1:TRA ON"], ["ASET"].
    """
    with _scope() as s:
        s.cmd(*commands)
    return f"sent {len(commands)} command(s)"


@mcp.tool()
def scope_autoset() -> str:
    """Run auto setup (ASET) — equivalent to the front-panel Auto button."""
    with _scope() as s:
        s.cmd("ASET")
    return "autoset issued"


@mcp.tool()
def scope_grab(channel: int = 1, span_ms: float = 3.0,
               points: int = 100000, save_npz: str | None = None,
               plot: bool = False) -> dict | list:
    """Pull raw sample memory (immune to display scaling) and summarize it.

    Returns point count, time span, min/max volts, the fraction of samples
    in the upper half ('high_pct' — a duty proxy), and the live acquisition
    state ('sample_status' from SAST? plus a 'running' bool) so you can tell
    a fresh frame from a frozen one on a STOPped scope. If acquisition memory
    is empty (scope STOPped before any frame was captured) it returns
    {points: 0, running: False, note: ...} rather than erroring out.
    Optionally saves the full t/v arrays to save_npz. Set plot=True to also
    return a rendered PNG of the trace, not just the scalars.
    """
    import numpy as np
    with _scope() as s:
        status = s.sample_status()
        try:
            t, v = s.grab(channel, span_ms / 1000, points)
        except NoWaveform as e:
            return {"points": 0, "running": False, "sample_status": status,
                    "note": str(e)}
    half = v.min() + (v.max() - v.min()) / 2
    result = {
        "points": int(len(v)),
        "running": status.strip().lower() != "stop",
        "sample_status": status,
        "span_ms": round(float(t[-1]) * 1000, 4),
        "vmin": round(float(v.min()), 4),
        "vmax": round(float(v.max()), 4),
        "vpp": round(float(v.max() - v.min()), 4),
        "high_pct": round(float((v > half).mean() * 100), 1),
    }
    if save_npz:
        np.savez(save_npz, t=t, v=v)
        result["saved"] = save_npz
    if plot:
        return [result, _plot_trace(t, v, f"C{channel}  {span_ms} ms")]
    return result


@mcp.tool()
def scope_save(slot: int) -> str:
    """Save the complete instrument setup to internal slot 1-20 (*SAV).

    Captures a reproducible bench config (channels, timebase, trigger,
    acquisition mode). Overwrites whatever was stored in that slot.
    """
    with _scope() as s:
        s.save(slot)
    return f"saved setup to slot {slot}"


@mcp.tool()
def scope_recall(slot: int) -> str:
    """Recall the instrument setup from internal slot 1-20 (*RCL)."""
    with _scope() as s:
        s.recall(slot)
    return f"recalled setup from slot {slot}"


@mcp.tool()
def scope_acquire(mode: str = "sampling", count: int = 16) -> dict:
    """Set the acquisition mode; return the resulting state.

    mode: 'sampling' (normal), 'peak' (peak-detect, catches narrow
    glitches), 'average' (averages a repetitive signal to cut noise), or
    'hires' (high-res / eres, oversampled bit depth). count applies only to
    'average' (4/16/32/64/128/256).

    ACQW is only honored while the scope is acquiring — a STOPped scope
    (e.g. just after a single-shot) ignores it, and AVERAGE/HIGH_RES are
    incompatible with single-shot. Returns {mode, avg_count, applied};
    applied is False when the readback didn't match the request.
    """
    with _scope() as s:
        return s.acq(mode, count=count)


@mcp.tool()
def scope_setup(channel: int = 1, vdiv: float | None = None,
                ofst: float | None = None, cpl: str | None = None,
                attn: str | None = None, bwl: bool | None = None,
                tdiv: str | None = None, trdl: str | None = None) -> dict:
    """Configure channel verticals and the global timebase; return state.

    Reproducible scaling without relying on autoset. vdiv/ofst in volts;
    cpl is a coupling code (D1M/A1M/D50/A50/GND); attn the probe factor
    ('0.1'/'1'/'10'/...); bwl toggles the 20 MHz limit; tdiv/trdl are the
    global timebase and trigger delay as strings ('200US', '5.0E-04', '0').
    Returns {vdiv, ofst, cpl, attn, bwl, tdiv, trdl}.
    """
    with _scope() as s:
        return s.setup(channel, vdiv=vdiv, ofst=ofst, cpl=cpl, attn=attn,
                       bwl=bwl, tdiv=tdiv, trdl=trdl)


@mcp.tool()
def scope_trigger(channel: int = 1, slope: str | None = None,
                  level: float | None = None, set50: bool = False,
                  mode: str | None = None, type: str = "EDGE") -> dict:
    """Configure the trigger and return the resulting state.

    Selects `type` (default EDGE) with source C<channel> and holdoff off,
    then applies the optional pieces: slope ('pos'/'neg'), level in volts
    or set50=True (level to 50% of the signal; set50 wins if both given),
    and mode ('auto'/'norm'/'single'/'stop'). Pair with scope_single for
    an unattended arm->wait->capture. Traps: SET50 is a no-op under
    dual-level triggers (e.g. Runt); TRLV is clamped to ~+-4.5 div.
    Returns {select, slope, level, mode}.
    """
    with _scope() as s:
        return s.trig(channel, slope=slope, level=level, set50=set50,
                      mode=mode, ttype=type)


@mcp.tool()
def scope_single(channel: int = 1, span_ms: float = 3.0, points: int = 100000,
                 timeout: float = 30.0, save_npz: str | None = None,
                 plot: bool = False) -> dict | list:
    """Arm a single-shot acquisition, wait for the trigger, then grab.

    Sets TRMD SINGLE and polls INR? until the acquisition completes (bit 0),
    then returns the same summary as scope_grab. The trigger (TRSE/TRLV/TRSL)
    must already be configured — set it first with scope_trigger. The scope
    is left STOPped on the captured frame. Returns {triggered: False} plus a
    diagnosis if no trigger fires within `timeout` seconds; producing that
    diagnosis free-runs the scope, so on timeout it is left in TRMD AUTO, not
    armed. Set plot=True to also return a rendered PNG of the captured frame.
    """
    import numpy as np
    with _scope() as s:
        result = s.single(channel, span_ms / 1000, points, timeout=timeout)
        if result is None:
            slope = s.query(f"C{channel}:TRSL?")
            out = {"triggered": False, "timeout_s": timeout}
            out.update(_diagnose_no_trigger(s, channel, slope))
            return out
    t, v = result
    half = v.min() + (v.max() - v.min()) / 2
    out = {
        "triggered": True,
        "points": int(len(v)),
        "span_ms": round(float(t[-1]) * 1000, 4),
        "vmin": round(float(v.min()), 4),
        "vmax": round(float(v.max()), 4),
        "vpp": round(float(v.max() - v.min()), 4),
        "high_pct": round(float((v > half).mean() * 100), 1),
    }
    if save_npz:
        np.savez(save_npz, t=t, v=v)
        out["saved"] = save_npz
    if plot:
        return [out, _plot_trace(t, v, f"C{channel} single-shot  {span_ms} ms")]
    return out


@mcp.tool()
def scope_capture(channel: int = 1, vdiv: float | None = None,
                  ofst: float | None = None, tdiv: str | None = None,
                  trdl: str | None = None, cpl: str | None = None,
                  attn: str | None = None, bwl: bool | None = None,
                  acq: str | None = None, slope: str = "pos",
                  level: float | None = None, set50: bool | None = None,
                  timeout: float = 30.0, span_ms: float = 3.0,
                  points: int = 100000, save_npz: str | None = None,
                  plot: bool = False) -> dict | list:
    """One-call configure -> arm -> capture, on a single connection.

    Applies channel/timebase setup (only the params you pass), an optional
    acquisition mode, an edge-trigger config, then single-shots and returns
    the same summary as scope_single. Collapses the scope_setup/scope_trigger/
    scope_acquire/scope_single round-trips into one tool call.

    Trigger defaults to a 50%% edge on `channel` (slope 'pos'); pass `level`
    for an absolute level, or set50=False to leave the level as-is. `acq`
    should be 'sampling' or 'peak' for single-shot — 'average'/'hires' need
    continuous acquisition and won't survive arming. Set plot=True to also
    return a PNG of the captured frame. Returns {triggered: False} plus a
    diagnosis on timeout (which leaves the scope free-running in TRMD AUTO).
    """
    import numpy as np
    if level is None and set50 is None:
        set50 = True                       # default: trigger at 50%
    acq_state = None
    with _scope() as s:
        if any(x is not None for x in (vdiv, ofst, tdiv, trdl, cpl, attn, bwl)):
            s.setup(channel, vdiv=vdiv, ofst=ofst, cpl=cpl, attn=attn,
                    bwl=bwl, tdiv=tdiv, trdl=trdl)
        if acq:
            # ACQW is only honored while acquiring — start running and let it
            # settle before applying the mode, then configure the trigger
            # (which leaves the run mode alone) so it sticks through arming.
            s.cmd("TRMD AUTO")
            time.sleep(0.4)
            acq_state = s.acq(acq)
        s.trig(channel, slope=slope, level=level, set50=bool(set50))
        result = s.single(channel, span_ms / 1000, points, timeout=timeout)
        if result is None:
            out = {"triggered": False, "timeout_s": timeout}
            out.update(_diagnose_no_trigger(s, channel, slope))
            if acq_state is not None and not acq_state["applied"]:
                out["acq_warning"] = "requested acq mode did not apply"
            return out
    t, v = result
    half = v.min() + (v.max() - v.min()) / 2
    out = {
        "triggered": True,
        "points": int(len(v)),
        "span_ms": round(float(t[-1]) * 1000, 4),
        "vmin": round(float(v.min()), 4),
        "vmax": round(float(v.max()), 4),
        "vpp": round(float(v.max() - v.min()), 4),
        "high_pct": round(float((v > half).mean() * 100), 1),
    }
    if acq_state is not None:
        out["acq"] = acq_state["mode"]
        if not acq_state["applied"]:
            out["acq_warning"] = "requested acq mode did not apply"
    if save_npz:
        np.savez(save_npz, t=t, v=v)
        out["saved"] = save_npz
    if plot:
        return [out, _plot_trace(t, v, f"C{channel} capture  {span_ms} ms")]
    return out


@mcp.tool()
def scope_math(span_ms: float = 3.0, points: int = 100000,
               define: str | None = None, save_npz: str | None = None,
               plot: bool = False) -> dict | list:
    """Read the MATH trace (non-FFT) and summarize it.

    Like scope_grab but for the math waveform: vertical scaling from MTVD
    (math V/div) and MTVP (position). FFT is NOT retrievable over WF?.
    define optionally sets the equation first, e.g. 'C1-C2', 'C1*C2' — a
    math function must be active or there is nothing to read. (Reference/
    REF traces are not WF?-readable on this scope; control them via raw
    scope_command REFSR/REFSA/etc.) Returns points, span, min/max/pp volts.
    Set plot=True to also return a rendered PNG of the math trace.
    """
    import numpy as np
    with _scope() as s:
        t, v = s.math(span_ms / 1000, points, define=define)
    result = {
        "points": int(len(v)),
        "span_ms": round(float(t[-1]) * 1000, 4),
        "vmin": round(float(v.min()), 4),
        "vmax": round(float(v.max()), 4),
        "vpp": round(float(v.max() - v.min()), 4),
    }
    if save_npz:
        np.savez(save_npz, t=t, v=v)
        result["saved"] = save_npz
    if plot:
        return [result, _plot_trace(t, v, f"MATH {define or ''}  {span_ms} ms")]
    return result


@mcp.tool()
def scope_screenshot() -> Image:
    """Capture the 800x480 display as a PNG image (requires ffmpeg)."""
    path = os.path.join(tempfile.gettempdir(), "scope_mcp_shot.png")
    with _scope() as s:
        s.screenshot(path)
    return Image(path=path)


@mcp.tool()
def scope_split(top: int = 1, bottom: int = 2) -> str:
    """Place two 0-5 V signals in separate screen halves (no overlap)."""
    with _scope() as s:
        s.split(top, bottom)
    return f"C{top} top, C{bottom} bottom"


@mcp.tool()
def scope_watch(channel: int, item: str, condition: str,
                timeout: float = 60.0) -> dict:
    """Poll a measurement until `value <op> threshold` holds for 3 samples.

    condition: a comparison like '>3', '<0.5', '>=3.3', '!=0' (operators
    >= <= == != > <). Blocks up to `timeout` seconds.
    Returns {met: bool, last: value}.
    """
    op, threshold = parse_condition(condition)
    with _scope() as s:
        met = s.watch(channel, item.upper(), op, threshold, timeout=timeout)
        last = s.meas(channel, item.upper())
    return {"met": met, "last": last}


if __name__ == "__main__":
    mcp.run()
