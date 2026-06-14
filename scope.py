#!/usr/bin/env python3
"""Bench control for the Siglent SDS1202X-E over LAN SCPI.

Library:   from scope import Scope; s = Scope(); s.meas(1, "PKPK")
CLI:       scope.py idn
           scope.py q "C1:VDIV?"
           scope.py c "TDIV 200US" "C1:TRA ON"
           scope.py meas C1 PKPK MEAN DUTY FREQ
           scope.py meas C1 all
           scope.py shot /tmp/screen.png
           scope.py grab C1 --span-ms 3 --out /tmp/c1.npz --plot /tmp/c1.png
           scope.py math --def 'C1-C2' --plot /tmp/math.png   # MATH trace (non-FFT)
           scope.py setup C1 --vdiv 1 --ofst -2.5 --cpl D1M --tdiv 200US
           scope.py acq average --count 16            # acquisition mode
           scope.py save 5                            # store full setup to slot 5
           scope.py recall 5                          # restore setup from slot 5
           scope.py trig C1 --slope pos --level 2.5    # edge trigger setup
           scope.py trig C1 --slope pos --set50 --mode norm
           scope.py single C1 --timeout 30 --out /tmp/shot.npz   # arm, wait, capture
           scope.py watch C1 PKPK ">3" --timeout 120 --say "contact"
           scope.py split C1 C2          # two 0-5V signals, no overlap
           scope.py aset
           scope.py dash                 # live terminal dashboard, Ctrl-C quits
           scope.py gui                  # live matplotlib window, mirrors the screen

Verified recipes and gotchas: see scope-remote.org.
"""

import argparse
import operator
import subprocess
import sys
import time

import pyvisa

DEFAULT_IP = "192.168.1.139"
BMP_SIZE = 768066


class NoWaveform(RuntimeError):
    """Raised by grab()/math() when the scope returns an empty frame."""
PIPER = ("~/PY3/bin/piper --model ~/.local/share/piper-voices/"
         "en_US-lessac-medium.onnx --length_scale 1.35 --sentence_silence 0.5")


def say(text):
    subprocess.run(
        f'echo "{text}" | {PIPER} --output_file /tmp/scope_say.wav 2>/dev/null'
        f' && aplay -q /tmp/scope_say.wav', shell=True)


COMPARATORS = {">=": operator.ge, "<=": operator.le, "==": operator.eq,
               "!=": operator.ne, ">": operator.gt, "<": operator.lt}


def parse_condition(cond):
    """Split a watch condition like '>3', '<0.5', '>=3.3', '!=0' into
    (op_string, threshold). Two-char operators are matched first so '>=' is
    not mistaken for '>'."""
    cond = cond.strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if cond.startswith(op):
            return op, float(cond[len(op):])
    raise ValueError(
        f"condition must start with one of >= <= == != > < : {cond!r}")


class Scope:
    """PyVISA-backed (pyvisa-py). Default resource is the LAN socket;
    pass e.g. "USB0::0xF4EC::..." to use USB-TMC instead."""

    def __init__(self, ip=DEFAULT_IP, timeout=10, resource=None):
        rm = pyvisa.ResourceManager("@py")
        self.inst = rm.open_resource(
            resource or f"TCPIP0::{ip}::5025::SOCKET",
            read_termination="\n", write_termination="\n",
            timeout=int(timeout * 1000))
        self.cmd("CHDR OFF")

    def close(self):
        self.inst.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def cmd(self, *commands):
        for c in commands:
            self.inst.write(c)

    def drain(self):
        """Discard any unread bytes left in the link by a previously aborted
        read (e.g. a desynced binary WF? transfer) — they would otherwise
        corrupt the next reply, and on this scope the dirt survives into a
        fresh connection. Reads with a short timeout until the link is empty.
        """
        saved = self.inst.timeout
        self.inst.timeout = 60
        try:
            while True:
                try:
                    self.inst.read_bytes(4096)
                except Exception:
                    break
        finally:
            self.inst.timeout = saved

    def query(self, q):
        return self.inst.query(q).strip()

    def num(self, q):
        """Query and parse the last comma field as float. None if '****'."""
        raw = self.query(q).split(",")[-1]
        for unit in ("Sa/s", "V", "S", "%", "Hz", "s"):
            raw = raw.removesuffix(unit)
        try:
            return float(raw)
        except ValueError:
            return None

    def meas(self, ch, what):
        return self.num(f"C{ch}:PAVA? {what}")

    def screenshot(self, path):
        """Save the 800x480 screen. .png needs ffmpeg, otherwise use .bmp."""
        self.cmd("SCDP")
        data = self.inst.read_bytes(BMP_SIZE)
        bmp = path if path.endswith(".bmp") else "/tmp/scope_scdp.bmp"
        with open(bmp, "wb") as out:
            out.write(data[:BMP_SIZE])
        if bmp != path:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-i", bmp, path], check=True)
        return path

    def _read_trace(self, source, span_s, points):
        """WFSU + <source>:WF? DAT2, parse the IEEE block. Returns
        (int8 code array, sparsing, sample rate). `source` is a WF? trace
        id: C1..C4 or MATH (non-FFT math; FFT is not retrievable here)."""
        import numpy as np
        sara = self.num("SARA?")
        sp = max(1, int(sara * span_s / points))
        self.cmd(f"WFSU SP,{sp},NP,{points},FP,0", f"{source}:WF? DAT2")
        # reply: [DAT2,]#9<9-digit length><int8 payload>\n\n
        buf = b""
        while b"#" not in buf:
            buf += self.inst.read_bytes(1)
        ndig = int(self.inst.read_bytes(1))
        length = int(self.inst.read_bytes(ndig))
        data = self.inst.read_bytes(length + 2)
        codes = np.frombuffer(data[:length], dtype=np.int8).astype(float)
        return codes, sp, sara

    def grab(self, ch, span_s=0.003, points=100000):
        """Raw sample readout. Returns (t_seconds, volts) numpy arrays.

        Raises NoWaveform if the scope returns an empty frame — that happens
        when acquisition memory holds nothing (the scope is STOPped before any
        frame was captured). Note a display-off channel (C<n>:TRA OFF) still
        returns its last acquired samples, so that is NOT an empty case. The
        message says how to recover instead of letting an empty array blow up
        downstream as a bare numpy 'zero-size reduction' error."""
        import numpy as np
        vdiv = self.num(f"C{ch}:VDIV?")
        ofst = self.num(f"C{ch}:OFST?")
        codes, sp, sara = self._read_trace(f"C{ch}", span_s, points)
        if len(codes) == 0:
            raise NoWaveform(
                f"C{ch}:WF? returned no samples — acquisition memory is empty "
                f"(scope STOPped before any frame was captured, "
                f"status={self.sample_status()!r}). Run it (TRMD AUTO) or arm "
                f"a single-shot before grabbing.")
        volts = codes * (vdiv / 25.0) - ofst
        t = np.arange(len(volts)) * sp / sara
        return t, volts

    def sample_status(self):
        """SAST? — the acquisition engine state: 'Stop', 'Ready', 'Trig'd',
        'Arm', 'Roll', etc. The reliable 'is the scope live' indicator;
        TRMD only says what was requested, not whether it is acquiring."""
        return self.query("SAST?")

    def running(self):
        """True when the scope is actively acquiring (not STOPped)."""
        return self.sample_status().strip().lower() != "stop"

    def math(self, span_s=0.003, points=100000, define=None):
        """Read the MATH trace as (t_seconds, volts) numpy arrays.

        Non-FFT math only — the FFT trace is not retrievable over WF?.
        Vertical scaling uses MTVD (math V/div) in place of a channel's
        VDIV; the offset comes from MTVP, the math vertical position in
        screen pixels where 50 px = 1 div (verified: a 50 px move shifts
        the codes by exactly 25, i.e. 25 codes/div as for analog).

        A MATH function must be active or MATH:WF? has nothing to return
        (and the empty reply desyncs the socket) — pass define (e.g.
        'C1-C2', 'C1*C2') to set the equation first via DEF EQN. Just after
        a redefine a boundary sample can momentarily read code -128 (=-5.12
        div * MTVD): that is the scope's genuine off-grid flag, not noise to
        filter — raise MTVD if the math result clips the grid.
        """
        import numpy as np
        if define:
            self.cmd(f"DEF EQN,'{define}'")
            time.sleep(0.5)
        mtvd = self.num("MTVD?")
        mtvp = self.num("MTVP?")
        codes, sp, sara = self._read_trace("MATH", span_s, points)
        if len(codes) == 0:
            raise NoWaveform(
                "MATH:WF? returned no samples — no MATH function is active "
                "or there is no acquired frame. Pass define=... to set an "
                "equation and run the scope before grabbing.")
        volts = codes * (mtvd / 25.0) - (mtvp / 50.0) * mtvd
        t = np.arange(len(volts)) * sp / sara
        return t, volts

    def single(self, ch, span_s=0.003, points=100000, timeout=30.0,
               poll=0.05, settle=0.2):
        """Arm one single-shot acquisition, wait for the trigger to fire,
        then grab the captured trace. Returns (t, volts), or None on timeout.

        Assumes the trigger (TRSE/TRLV/TRSL) is already configured. On the
        SDS1202X-E firmware INR? exposes only bit 0 (a new acquisition has
        completed) and bit 13 (armed); reading INR? clears the latch, and
        TRMD SINGLE itself resets bit 0 — so arm, then poll for bit 0 (no
        pre-clear, which would let an in-flight free-run acquisition trip a
        false positive). After bit 0 the single-shot has STOPped; wait a
        beat before reading. Grabbing the instant bit 0 latches can hit a
        not-yet-ready frame whose WF? reply lacks the '#' block header,
        which would desync the socket.
        """
        self.cmd("TRMD SINGLE")                # arm; this also resets bit 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                inr = int(self.query("INR?").split(",")[-1])
            except ValueError:
                inr = 0
            if inr & 1:                        # acquisition complete
                time.sleep(settle)             # let the frame settle
                return self.grab(ch, span_s, points)
            time.sleep(poll)
        return None

    def trig(self, ch, slope=None, level=None, set50=False, mode=None,
             ttype="EDGE"):
        """Configure the trigger and return the resulting state.

        Sends TRSE (select `ttype`, source C<ch>, holdoff off), then the
        optional pieces: TRSL slope (POS/NEG), level via SET50 (50% of the
        signal) or C<ch>:TRLV (absolute volts), and TRMD mode (AUTO/NORM/
        SINGLE/STOP). `set50` wins over `level` if both are given.

        Traps: SET50 is a no-op under dual-level triggers (e.g. Runt). TRLV
        is clamped by the scope to about +-4.5 div * the source's V/div.

        Sends each command with a small gap and settles before reading back:
        the scope's parser falls behind a back-to-back write burst over the
        LAN socket, and a query issued too soon times out and desyncs it.
        """
        commands = [f"TRSE {ttype},SR,C{ch},HT,OFF"]
        if slope:
            commands.append(f"C{ch}:TRSL {slope.upper()}")
        if set50:
            commands.append("SET50")
        elif level is not None:
            commands.append(f"C{ch}:TRLV {level}V")
        if mode:
            commands.append(f"TRMD {mode.upper()}")
        for c in commands:
            self.cmd(c)
            time.sleep(0.1)
        time.sleep(0.2)
        return self.trig_state(ch)

    def trig_state(self, ch):
        """Read back the trigger configuration as a dict."""
        return {"select": self.query("TRSE?"),
                "slope": self.query(f"C{ch}:TRSL?"),
                "level": self.num(f"C{ch}:TRLV?"),
                "mode": self.query("TRMD?")}

    def setup(self, ch, vdiv=None, ofst=None, cpl=None, attn=None, bwl=None,
              tdiv=None, trdl=None):
        """Configure channel C<ch> verticals and (globally) the timebase,
        then return the resulting state. Reproducible scaling without ASET.

        vdiv/ofst in volts; cpl is a coupling code (D1M/A1M/D50/A50/GND);
        attn is the probe factor (0.1/1/10/100...); bwl toggles the 20 MHz
        limit (True/False). tdiv/trdl are global, passed through as strings
        (e.g. '200US', '5.0E-04', '0').

        BWL uses the legacy global form (BWL C<ch>,ON) — the per-channel
        C<ch>:BWL set form is silently ignored on this firmware. Writes are
        spaced and settled before readback (the LAN parser lags a burst).

        ATTN is applied before VDIV/OFST: the probe factor rescales the
        vertical scale and offset (they are probe-referred volts), so the
        requested vdiv/ofst only land if the attenuation is set first.
        """
        commands = []
        if attn is not None:
            commands.append(f"C{ch}:ATTN {attn}")
        if cpl:
            commands.append(f"C{ch}:CPL {cpl.upper()}")
        if vdiv is not None:
            commands.append(f"C{ch}:VDIV {vdiv}V")
        if ofst is not None:
            commands.append(f"C{ch}:OFST {ofst}V")
        if bwl is not None:
            commands.append(f"BWL C{ch},{'ON' if bwl else 'OFF'}")
        if tdiv:
            commands.append(f"TDIV {tdiv}")
        if trdl is not None:
            commands.append(f"TRDL {trdl}")
        for c in commands:
            self.cmd(c)
            time.sleep(0.1)
        time.sleep(0.2)
        return self.setup_state(ch)

    def setup_state(self, ch):
        """Read back the channel + timebase configuration as a dict."""
        return {"vdiv": self.num(f"C{ch}:VDIV?"),
                "ofst": self.num(f"C{ch}:OFST?"),
                "cpl": self.query(f"C{ch}:CPL?"),
                "attn": self.query(f"C{ch}:ATTN?"),
                "bwl": self.query(f"C{ch}:BWL?"),
                "tdiv": self.num("TDIV?"),
                "trdl": self.num("TRDL?")}

    ACQ_MODES = {"sampling": "SAMPLING",
                 "peak": "PEAK_DETECT", "peak_detect": "PEAK_DETECT",
                 "average": "AVERAGE", "avg": "AVERAGE",
                 "hires": "HIGH_RES", "high_res": "HIGH_RES", "eres": "HIGH_RES"}

    def acq(self, mode, count=16):
        """Set the acquisition mode and return the resulting state.

        mode: 'sampling' (normal), 'peak' (peak-detect, catches narrow
        glitches), 'average' (averages a repetitive signal to cut noise),
        or 'hires' (high-res / eres, oversampled bit depth). For 'average',
        count is applied as ACQW AVERAGE,<count> (4/16/32/64/128/256) and is
        only meaningful in AVERAGE mode — AVGA? retains it across modes.

        ACQW is only honored while the scope is acquiring: a STOPped scope
        (e.g. just after a single-shot) silently ignores the write. AVERAGE
        and HIGH_RES also need multiple/continuous acquisitions, so they are
        incompatible with single-shot. The returned dict carries 'applied':
        False when the readback doesn't match the request.
        """
        acqw = self.ACQ_MODES[mode.lower()]
        self.cmd(f"ACQW AVERAGE,{count}" if acqw == "AVERAGE"
                 else f"ACQW {acqw}")
        time.sleep(0.2)
        state = self.acq_state()
        state["applied"] = state["mode"].split(",")[0] == acqw
        return state

    def acq_state(self):
        """Read back the acquisition mode and average count as a dict."""
        return {"mode": self.query("ACQW?"), "avg_count": self.num("AVGA?")}

    def save(self, slot):
        """Save the complete instrument setup to internal slot 1-20 (*SAV)."""
        if not 1 <= int(slot) <= 20:
            raise ValueError("slot must be 1-20")
        self.cmd(f"*SAV {int(slot)}")

    def recall(self, slot):
        """Recall the instrument setup from internal slot 1-20 (*RCL)."""
        if not 1 <= int(slot) <= 20:
            raise ValueError("slot must be 1-20")
        self.cmd(f"*RCL {int(slot)}")

    def status(self):
        """Full instrument-state snapshot as a nested dict. Queries only
        (no writes), so it is safe to call at any time without disturbing
        the scope or risking the write-burst desync."""
        trse = self.query("TRSE?")
        parts = trse.split(",")
        trig = {"select": trse, "mode": self.query("TRMD?")}
        src = parts[parts.index("SR") + 1] if "SR" in parts else None
        if src and src.startswith("C"):
            trig["source"] = src
            trig["slope"] = self.query(f"{src}:TRSL?")
            trig["level"] = self.num(f"{src}:TRLV?")
        chans = {}
        for ch in (1, 2):
            chans[f"C{ch}"] = {
                "on": self.query(f"C{ch}:TRA?"),
                "vdiv": self.num(f"C{ch}:VDIV?"),
                "ofst": self.num(f"C{ch}:OFST?"),
                "cpl": self.query(f"C{ch}:CPL?"),
                "attn": self.query(f"C{ch}:ATTN?"),
                "bwl": self.query(f"C{ch}:BWL?"),
            }
        return {
            "idn": self.query("*IDN?"),
            "timebase": {"tdiv": self.num("TDIV?"), "trdl": self.num("TRDL?"),
                         "sara": self.num("SARA?")},
            "trigger": trig,
            "acquire": self.acq_state(),
            "channels": chans,
        }

    def split(self, ch_top, ch_bottom, vmax=5.0):
        """Place two 0..vmax signals in separate screen halves."""
        vdiv = vmax / 2.5
        self.cmd(f"C{ch_top}:TRA ON", f"C{ch_top}:VDIV {vdiv}V",
                 f"C{ch_top}:OFST {vdiv / 2}V",
                 f"C{ch_bottom}:TRA ON", f"C{ch_bottom}:VDIV {vdiv}V",
                 f"C{ch_bottom}:OFST {-3 * vdiv}V")

    def watch(self, ch, what, op, threshold, timeout=120, settle=3,
              interval=1.5, announce=None):
        """Poll a measurement until `value <op> threshold` holds for
        `settle` consecutive samples. Returns True on success. `op` is any
        key of COMPARATORS (>= <= == != > <)."""
        compare = COMPARATORS[op]
        deadline = time.time() + timeout
        streak = 0
        while time.time() < deadline:
            value = self.meas(ch, what)
            print(f"C{ch} {what} = {value}", flush=True)
            streak = streak + 1 if (
                value is not None and compare(value, threshold)) else 0
            if streak >= settle:
                if announce:
                    say(announce)
                return True
            time.sleep(interval)
        return False


BLOCKS = " ▁▂▃▄▅▆▇█"


def report_wave(t, v, out=None, plot=None):
    """Print a one-line summary of a grabbed trace; optionally save .npz / .png."""
    high = (v > v.min() + (v.max() - v.min()) / 2).mean() * 100
    print(f"{len(v)} pts, {t[-1]*1000:.2f} ms, "
          f"{v.min():.3f}..{v.max():.3f} V, high {high:.1f}%")
    if out:
        import numpy as np
        np.savez(out, t=t, v=v)
        print("data:", out)
    if plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 3.5))
        ax.plot(t * 1000, v, lw=0.8)
        ax.set_xlabel("ms")
        ax.set_ylabel("V")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot, dpi=110)
        print("plot:", plot)


def sparkline(volts, width, vmin, vmax):
    import numpy as np
    if len(volts) < width:
        return ""
    span = max(vmax - vmin, 1e-9)
    cols = np.array_split(volts, width)
    line_hi, line_lo = [], []
    for c in cols:
        top = (c.max() - vmin) / span
        bot = (c.min() - vmin) / span
        # two text rows = 16 levels; draw the column's max in the
        # upper row and its min in the lower so edges stay visible
        hi = int(round(top * 16))
        lo = int(round(bot * 16))
        line_hi.append(BLOCKS[min(max(hi - 8, 0), 8)])
        line_lo.append(BLOCKS[8] if hi > 8 and lo < 8 else
                       BLOCKS[min(max(max(hi, lo), 0), 8)] if hi <= 8 else
                       BLOCKS[min(max(lo, 0), 8)])
    return "".join(line_hi) + "\n" + "".join(line_lo)


def dashboard(scope, channels=(1, 2), interval=1.0):
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    colors = {1: "yellow", 2: "magenta", 3: "cyan", 4: "green"}
    color = lambda ch: colors.get(ch, "white")
    wave_cache = {}
    last_grab = 0.0

    def snapshot():
        nonlocal last_grab
        items = ("PKPK", "MEAN", "FREQ", "DUTY", "MAX", "MIN")
        table = Table(title=f"SDS1202X-E  {time.strftime('%H:%M:%S')}",
                      expand=True)
        table.add_column("")
        for item in items:
            table.add_column(item, justify="right")
        for ch in channels:
            row = [scope.meas(ch, item) for item in items]
            table.add_row(
                Text(f"CH{ch}", style=f"bold {color(ch)}"),
                *[("—" if v is None else f"{v:g}") for v in row])
        if time.time() - last_grab > 3:
            for ch in channels:
                # grab unconditionally and catch the empty-frame case, so a
                # scope STOPped on a good frame still renders (its live PAVA
                # would read None) instead of being gated out as "no signal".
                try:
                    t, v = scope.grab(ch, span_s=0.003, points=3000)
                    wave_cache[ch] = sparkline(v, 76, v.min(), v.max())
                except NoWaveform:
                    wave_cache[ch] = "(no frame)"
            last_grab = time.time()
        panels = [table]
        for ch in channels:
            panels.append(Panel(Text(wave_cache.get(ch, ""),
                                     style=color(ch)),
                                title=f"CH{ch} — last 3 ms", padding=0))
        return Group(*panels)

    if not sys.stdout.isatty():
        from rich.console import Console
        Console().print(snapshot())
        return
    with Live(snapshot(), refresh_per_second=4, screen=True) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(snapshot())
        except KeyboardInterrupt:
            pass


def gui(scope, channels=(1, 2), span_ms=7.0, points=1400, interval_ms=200):
    """Live waveform window mirroring the scope screen: real traces,
    dark theme, scope-coloured channels, per-channel readout in the title."""
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    colors = {1: "#e8e800", 2: "#e000e0", 3: "#00d0e0", 4: "#00d000"}
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(11, 5))
    fig.canvas.manager.set_window_title("SDS1202X-E — live")
    lines = {ch: ax.plot([], [], color=colors.get(ch, "#cccccc"), lw=1.0,
                         label=f"CH{ch}")[0]
             for ch in channels}
    ax.set_xlabel("ms")
    ax.set_ylabel("V")
    ax.grid(True, which="both", color="#303030", lw=0.6)
    ax.legend(loc="upper right")
    span_s = span_ms / 1000.0
    state = {"meas": {ch: "" for ch in channels}, "n": 0}

    def update(_):
        lo, hi = [], []
        for ch in channels:
            try:                                # empty frame: blank this line
                t, v = scope.grab(ch, span_s=span_s, points=points)
            except NoWaveform:
                lines[ch].set_data([], [])
                continue
            lines[ch].set_data(t * 1000.0, v)
            lo.append(float(v.min()))
            hi.append(float(v.max()))
        if state["n"] % 5 == 0:                 # throttle SCPI measurements
            for ch in channels:
                f = scope.meas(ch, "FREQ")
                d = scope.meas(ch, "DUTY")
                p = scope.meas(ch, "PKPK")
                state["meas"][ch] = (
                    f"CH{ch} {f/1000:.3f}kHz {d:.0f}% {p:.2f}Vpp"
                    if None not in (f, d, p) else f"CH{ch} —")
        state["n"] += 1
        ax.set_xlim(0, span_ms)
        if lo:                                  # at least one live channel
            ymin, ymax = min(lo), max(hi)
            pad = (ymax - ymin) * 0.1 or 0.5
            ax.set_ylim(ymin - pad, ymax + pad)
        ax.set_title("    ".join(state["meas"][ch] for ch in channels),
                     fontsize=11)
        return list(lines.values())

    anim = FuncAnimation(fig, update, interval=interval_ms, blit=False,
                         cache_frame_data=False)
    fig._anim = anim                            # keep a reference alive
    plt.show()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=DEFAULT_IP)
    p.add_argument("--resource", help="full VISA resource string (overrides --ip)")
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("idn")
    sub.add_parser("aset")
    q = sub.add_parser("q")
    q.add_argument("query")
    c = sub.add_parser("c")
    c.add_argument("commands", nargs="+")
    m = sub.add_parser("meas")
    m.add_argument("channel")
    m.add_argument("items", nargs="+")
    sh = sub.add_parser("shot")
    sh.add_argument("path")
    g = sub.add_parser("grab")
    g.add_argument("channel")
    g.add_argument("--span-ms", type=float, default=3.0)
    g.add_argument("--points", type=int, default=100000)
    g.add_argument("--out")
    g.add_argument("--plot")
    mt = sub.add_parser("math")
    mt.add_argument("--def", dest="equation",
                    help="set the math equation first, e.g. 'C1-C2'")
    mt.add_argument("--span-ms", type=float, default=3.0)
    mt.add_argument("--points", type=int, default=100000)
    mt.add_argument("--out")
    mt.add_argument("--plot")
    si = sub.add_parser("single")
    si.add_argument("channel")
    si.add_argument("--span-ms", type=float, default=3.0)
    si.add_argument("--points", type=int, default=100000)
    si.add_argument("--timeout", type=float, default=30.0)
    si.add_argument("--out")
    si.add_argument("--plot")
    si.add_argument("--say")
    tr = sub.add_parser("trig")
    tr.add_argument("channel")
    tr.add_argument("--slope", choices=["pos", "neg"])
    tr.add_argument("--level", type=float, help="trigger level in volts")
    tr.add_argument("--set50", action="store_true", help="set level to 50%%")
    tr.add_argument("--mode", choices=["auto", "norm", "single", "stop"])
    tr.add_argument("--type", default="EDGE")
    st = sub.add_parser("setup")
    st.add_argument("channel")
    st.add_argument("--vdiv", type=float, help="volts/div")
    st.add_argument("--ofst", type=float, help="vertical offset, volts")
    st.add_argument("--cpl", help="coupling: D1M A1M D50 A50 GND")
    st.add_argument("--attn", help="probe factor: 0.1 1 10 100 ...")
    st.add_argument("--bwl", choices=["on", "off"], help="20 MHz bandwidth limit")
    st.add_argument("--tdiv", help="timebase, e.g. 200US or 5.0E-04")
    st.add_argument("--trdl", help="trigger delay, e.g. 0 or -1MS")
    ac = sub.add_parser("acq")
    ac.add_argument("mode", choices=["sampling", "peak", "average", "hires"])
    ac.add_argument("--count", type=int, default=16,
                    help="averages for AVERAGE mode: 4,16,32,64,128,256")
    sv = sub.add_parser("save")
    sv.add_argument("slot", type=int, choices=range(1, 21), metavar="SLOT")
    rc = sub.add_parser("recall")
    rc.add_argument("slot", type=int, choices=range(1, 21), metavar="SLOT")
    w = sub.add_parser("watch")
    w.add_argument("channel")
    w.add_argument("item")
    w.add_argument("condition", help='e.g. ">3" or "<0.5"')
    w.add_argument("--timeout", type=float, default=120)
    w.add_argument("--say")
    sp = sub.add_parser("split")
    sp.add_argument("top")
    sp.add_argument("bottom")
    d = sub.add_parser("dash")
    d.add_argument("--interval", type=float, default=1.0)
    d.add_argument("--channels", default="1,2", help="comma-separated, e.g. 1 or 1,2")
    gu = sub.add_parser("gui")
    gu.add_argument("--span-ms", type=float, default=7.0)
    gu.add_argument("--points", type=int, default=1400)
    gu.add_argument("--channels", default="1,2", help="comma-separated, e.g. 1 or 1,2")
    args = p.parse_args()

    ch = lambda name: name.upper().lstrip("C")
    chans = lambda s: tuple(int(c.strip().upper().lstrip("C")) for c in s.split(","))
    with Scope(args.ip, resource=args.resource) as s:
        if args.verb == "idn":
            print(s.query("*IDN?"))
        elif args.verb == "aset":
            s.cmd("ASET")
        elif args.verb == "q":
            print(s.query(args.query))
        elif args.verb == "c":
            s.cmd(*args.commands)
        elif args.verb == "meas":
            items = ["MEAN", "PKPK", "FREQ", "DUTY", "MAX", "MIN"] \
                if args.items == ["all"] else [i.upper() for i in args.items]
            for item in items:
                print(f"{item:5s} {s.meas(ch(args.channel), item)}")
        elif args.verb == "shot":
            print(s.screenshot(args.path))
        elif args.verb == "grab":
            t, v = s.grab(ch(args.channel), args.span_ms / 1000, args.points)
            report_wave(t, v, args.out, args.plot)
        elif args.verb == "math":
            t, v = s.math(args.span_ms / 1000, args.points,
                          define=args.equation)
            report_wave(t, v, args.out, args.plot)
        elif args.verb == "single":
            result = s.single(ch(args.channel), args.span_ms / 1000,
                              args.points, timeout=args.timeout)
            if result is None:
                print(f"no trigger within {args.timeout:g}s", file=sys.stderr)
                sys.exit(1)
            report_wave(*result, args.out, args.plot)
            if args.say:
                say(args.say)
        elif args.verb == "trig":
            state = s.trig(ch(args.channel), slope=args.slope,
                           level=args.level, set50=args.set50,
                           mode=args.mode, ttype=args.type.upper())
            for k, v in state.items():
                print(f"{k:7s} {v}")
        elif args.verb == "setup":
            bwl = None if args.bwl is None else (args.bwl == "on")
            state = s.setup(ch(args.channel), vdiv=args.vdiv, ofst=args.ofst,
                            cpl=args.cpl, attn=args.attn, bwl=bwl,
                            tdiv=args.tdiv, trdl=args.trdl)
            for k, v in state.items():
                print(f"{k:5s} {v}")
        elif args.verb == "acq":
            state = s.acq(args.mode, count=args.count)
            for k, v in state.items():
                print(f"{k:9s} {v}")
            if not state["applied"]:
                print("warning: mode not applied — ACQW is ignored while the "
                      "scope is STOPped, and AVERAGE/HIGH_RES are incompatible "
                      "with single-shot", file=sys.stderr)
        elif args.verb == "save":
            s.save(args.slot)
            print(f"saved setup to slot {args.slot}")
        elif args.verb == "recall":
            s.recall(args.slot)
            print(f"recalled setup from slot {args.slot}")
        elif args.verb == "watch":
            op, threshold = parse_condition(args.condition)
            ok = s.watch(ch(args.channel), args.item.upper(), op, threshold,
                         timeout=args.timeout, announce=args.say)
            sys.exit(0 if ok else 1)
        elif args.verb == "split":
            s.split(ch(args.top), ch(args.bottom))
        elif args.verb == "dash":
            dashboard(s, channels=chans(args.channels), interval=args.interval)
        elif args.verb == "gui":
            gui(s, channels=chans(args.channels), span_ms=args.span_ms,
                points=args.points)


if __name__ == "__main__":
    main()
