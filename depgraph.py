"""
depgraph.py - a small dependency graph for a Verilog chip design.

I am learning chip design, and this is the first tool I wrote while reading through
PicoRV32. The idea from my notes: a chip is a pile of registers with logic between
them, and on every clock edge each register takes whatever the logic worked out.
So "if I touch this signal, what else can it affect?" is a plain graph question.

Yosys does the hard part. It reads the Verilog and writes a JSON netlist where every
wire bit has a number, every cell says which bits it reads and writes, and everything
carries the source line it came from. One command:

    yosys -q -p "read_verilog picorv32.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json picorv32.json"

Then:

    python3 depgraph.py picorv32.json stats
    python3 depgraph.py picorv32.json drivers decoded_imm    # what writes this signal
    python3 depgraph.py picorv32.json readers cpu_state      # what reads it
    python3 depgraph.py picorv32.json impact  reg_pc         # everything downstream of it
    python3 depgraph.py picorv32.json impact  reg_pc --tapeout
    python3 depgraph.py before.json diff after.json          # signals added and removed
    python3 depgraph.py picorv32.json dot    cpu_state        # graphviz picture of one signal
    python3 depgraph.py core.json     verify clean.vcd bug.vcd --line 1339
                                                             # check the prediction against a real simulation
"""
import json
import re
import sys

# Cell types that hold a value until the next clock edge, ie. flip-flops and latches.
# Everything else is combinational: it settles to a new answer as soon as its inputs move.
REGISTER_TYPES = {"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe", "$dffsr", "$dlatch", "$adlatch",
                  "$memwr", "$memwr_v2"}   # a write into a memory is state too


def src_lines(obj):
    """Every source line a cell or wire came from. Yosys can list several ranges joined by '|'."""
    out = set()
    for part in obj.get("attributes", {}).get("src", "").split("|"):
        m = re.search(r":(\d+)\.\d+-(\d+)\.\d+$", part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
    return out


def where(obj):
    """Yosys writes 'picorv32.v:1007.2-1120.5' into the src attribute. Keep 'picorv32.v:1007'."""
    src = obj.get("attributes", {}).get("src", "").split("|")[0]
    if ":" not in src:
        return "?"
    path, rest = src.rsplit(":", 1)
    return path.split("/")[-1] + ":" + rest.split(".")[0]


def load(path):
    design = json.load(open(path))
    top_name, top = next(iter(design["modules"].items()))   # after flatten there is only the top module

    signals = {}        # name you can type -> bits and source line
    names_of = {}       # bit number -> names that include this bit
    for name, net in top["netnames"].items():
        if net.get("hide_name"):        # Yosys temporaries like $procmux$4218, no use to a human
            continue
        bits = [b for b in net["bits"] if isinstance(b, int)]
        signals[name] = {"bits": bits, "where": where(net), "lines": src_lines(net)}
        for b in bits:
            names_of.setdefault(b, []).append(name)

    cells = []
    for cname, c in top["cells"].items():
        # a bit is an int for a wire, or a string "0"/"1"/"x" for a constant value
        conns = {port: (c["port_directions"].get(port, "input"), bits)
                 for port, bits in c["connections"].items()}
        ins = {b for d, bs in conns.values() if d != "output" for b in bs if isinstance(b, int)}
        outs = {b for d, bs in conns.values() if d == "output" for b in bs if isinstance(b, int)}
        cells.append({"type": c["type"], "where": where(c), "lines": src_lines(c), "conns": conns,
                      "memid": c.get("parameters", {}).get("MEMID"),
                      "ins": ins, "outs": outs, "is_register": c["type"] in REGISTER_TYPES})

    # Memories are the one thing not joined by wires. Yosys emits a separate write cell and read
    # cells that find each other through a MEMID parameter, so a plain walk stops dead at the
    # register file. Give each memory one invented wire (negative, so it cannot clash with a real
    # bit number) that the write drives and the reads depend on.
    invented = {}
    for c in cells:
        if not c["memid"]:
            continue
        b = invented.setdefault(c["memid"], -len(invented) - 1)
        if c["type"].startswith("$memwr"):
            c["outs"].add(b)
        elif c["type"].startswith("$memrd"):
            c["ins"].add(b)

    writers, readers = {}, {}
    for i, c in enumerate(cells):
        for b in c["outs"]:
            writers.setdefault(b, []).append(i)
        for b in c["ins"]:
            readers.setdefault(b, []).append(i)

    def ports(direction):
        return {p: [b for b in info["bits"] if isinstance(b, int)]
                for p, info in top["ports"].items() if info["direction"] == direction}
    return {"top": top_name, "signals": signals, "names_of": names_of, "cells": cells,
            "writers": writers, "readers": readers, "outputs": ports("output"), "inputs": ports("input")}


def find(d, wanted):
    """After flatten a name looks like 'picorv32_core.decoded_imm'. Let people type the short part."""
    if wanted in d["signals"]:
        return wanted
    hits = [n for n in d["signals"] if n.split(".")[-1] == wanted]
    if not hits:
        sys.exit("no signal called " + wanted)
    if len(hits) > 1:
        sys.exit(wanted + " is ambiguous: " + ", ".join(hits))
    return hits[0]


def describe(d, bits):
    """Say what a port is wired to: a signal name, a constant value, or a count of unnamed wires."""
    if bits and all(isinstance(b, str) for b in bits):
        # Yosys lists constants most-significant bit last, so reverse before reading them off
        value = "".join(reversed([b if b in "01" else "x" for b in bits]))
        return "constant 0b" + value if "x" in value else "constant %d" % int(value, 2)
    wires = [b for b in bits if isinstance(b, int)]
    names = sorted({n for b in wires for n in d["names_of"].get(b, [])})
    if names:
        return ", ".join(names)
    return "%d unnamed wire%s" % (len(wires), "" if len(wires) == 1 else "s")


def show_cell(d, i):
    """Print one cell and every port it has, so you can see exactly what it is wired to."""
    c = d["cells"][i]
    print("  %s %s at %s" % ("register" if c["is_register"] else "logic", c["type"], c["where"]))
    for port, (direction, bits) in c["conns"].items():
        if bits:
            print("      %-4s %s %s" % (port, "->" if direction == "output" else "<-", describe(d, bits)))


def stats(d):
    regs = sum(1 for c in d["cells"] if c["is_register"])
    print("top module:", d["top"])
    print("named signals:", len(d["signals"]))
    print("cells:", len(d["cells"]), "(%d registers, %d combinational)" % (regs, len(d["cells"]) - regs))
    print("chip outputs:", len(d["outputs"]))


def drivers(d, name):
    sig = d["signals"][name]
    seen = []
    for b in sig["bits"]:
        for i in d["writers"].get(b, []):
            if i not in seen:
                seen.append(i)
    print("%s (%d bits, declared at %s) is written by %d cell(s):" % (name, len(sig["bits"]), sig["where"], len(seen)))
    for i in seen:
        show_cell(d, i)


def readers(d, name, limit=5):
    sig = d["signals"][name]
    seen = []
    for b in sig["bits"]:
        for i in d["readers"].get(b, []):
            if i not in seen:
                seen.append(i)
    print("%s (%d bits, declared at %s) is read by %d cell(s):" % (name, len(sig["bits"]), sig["where"], len(seen)))
    for i in seen[:limit]:
        show_cell(d, i)
    if len(seen) > limit:
        print("  ... and %d more" % (len(seen) - limit))


def reach(d, start_bits, stop_at_registers):
    """Walk forward: bit -> cells that read it -> bits they write -> and so on.

    stop_at_registers=True counts a register but does not walk past it, which answers
    "what moves in this same clock cycle". False answers "what can move eventually".
    """
    todo, bits_seen, cells_seen, regs_hit = list(start_bits), set(start_bits), set(), set()
    while todo:
        b = todo.pop()
        for i in d["readers"].get(b, []):
            if i in cells_seen:
                continue
            cells_seen.add(i)
            if d["cells"][i]["is_register"]:
                regs_hit.add(i)
                if stop_at_registers:
                    continue
            for out in d["cells"][i]["outs"]:
                if out not in bits_seen:
                    bits_seen.add(out)
                    todo.append(out)
    return bits_seen, cells_seen, regs_hit


def impact(d, name, as_tapeout=False):
    sig = d["signals"][name]
    _, same_cells, same_regs = reach(d, sig["bits"], stop_at_registers=True)
    all_bits, all_cells, _ = reach(d, sig["bits"], stop_at_registers=False)
    hit = [p for p, bits in d["outputs"].items() if any(b in all_bits for b in bits)]
    if as_tapeout:
        # The same answer written in Tapeout Labs' .tapeout format: one fact per line,
        # every line carrying the source it came from. github.com/tapeout-labs/tof
        print("tapeout 1")
        print("ip picorv32 top " + d["top"])
        print('finding IMPACT-001 confirmed info "%s reaches %d of %d chip outputs" '
              'desc "same cycle %d cells into %d registers; eventually %d of %d cells" @ %s'
              % (name.split(".")[-1], len(hit), len(d["outputs"]),
                 len(same_cells), len(same_regs), len(all_cells), len(d["cells"]), sig["where"]))
        return
    print("%s (%d bits, declared at %s)" % (name, len(sig["bits"]), sig["where"]))
    print("  same clock cycle: %d cells, landing in %d registers" % (len(same_cells), len(same_regs)))
    print("  across clock cycles: %d of %d cells" % (len(all_cells), len(d["cells"])))
    print("  chip outputs it can affect: %d of %d%s" % (len(hit), len(d["outputs"]), (" (" + ", ".join(hit) + ")") if hit else ""))


def diff(before, after):
    """Compare two versions by signal name. A rename shows up as one gone and one new,
    which is the honest limitation of comparing by name. See the README."""
    old = {n.split(".")[-1] for n in before["signals"]}
    new = {n.split(".")[-1] for n in after["signals"]}
    print("same name: %d   gone: %d   new: %d" % (len(old & new), len(old - new), len(new - old)))
    for n in sorted(old - new):
        print("  - " + n)
    for n in sorted(new - old):
        print("  + " + n)


def dot(d, name, hops=1):
    """Print a graphviz graph of the cells around one signal. Render with:
       python3 depgraph.py picorv32.json dot cpu_state > g.dot && dot -Tpng g.dot -o g.png
    """
    sig = d["signals"][name]
    bits = set(sig["bits"])
    cells = set()
    for b in bits:
        cells.update(d["writers"].get(b, []))
        cells.update(d["readers"].get(b, []))
    for _ in range(hops - 1):
        for i in list(cells):
            for b in d["cells"][i]["ins"] | d["cells"][i]["outs"]:
                cells.update(d["writers"].get(b, []))
    short = name.split(".")[-1]
    print("digraph g {")
    print('  rankdir=LR; node [shape=box, fontname="Helvetica", fontsize=10];')
    print('  "%s" [shape=ellipse, style=filled, fillcolor=lightgrey];' % short)
    for i in sorted(cells):
        c = d["cells"][i]
        label = "%s\\n%s" % (c["type"].lstrip("$"), c["where"])
        print('  c%d [label="%s"%s];' % (i, label, ', style=filled, fillcolor=lightblue' if c["is_register"] else ""))
        if bits & c["outs"]:
            print('  c%d -> "%s";' % (i, short))
        if bits & c["ins"]:
            print('  "%s" -> c%d;' % (short, i))
    print("}")


def bits_from_lines(d, lines):
    """The wires written by anything that came from the given source lines."""
    start = set()
    for c in d["cells"]:
        if c["lines"] & lines:
            start.update(c["outs"])
    for sig in d["signals"].values():
        if sig["lines"] & lines:
            start.update(sig["bits"])
    return start


def read_vcd(path):
    """Read a VCD waveform file into {signal name: [(time, value), ...]}.

    A VCD is a header that maps short id codes to signal names, then a list of
    timestamps and the value changes that happened at each one.
    """
    codes, scope, traces, t, in_header = {}, [], {}, 0, True
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        if in_header:
            if line.startswith("$scope"):
                scope.append(line.split()[2])
            elif line.startswith("$upscope"):
                scope and scope.pop()
            elif line.startswith("$var"):
                bits = line.split()
                codes.setdefault(bits[3], []).append(".".join(scope + [bits[4]]))
            elif line.startswith("$enddefinitions"):
                in_header = False
            continue
        if line.startswith("#"):
            t = int(line[1:])
        elif line[0] in "bB":
            value, _, code = line[1:].partition(" ")
            for n in codes.get(code.strip(), []):
                traces.setdefault(n, []).append((t, value))
        elif line[0] not in "rR$":
            for n in codes.get(line[1:], []):
                traces.setdefault(n, []).append((t, line[0]))
    return traces


def verify(d, before_vcd, after_vcd, lines, prefix="testbench.uut."):
    """Check the graph's prediction against two real simulation runs.

    Give it the lines that changed between the runs. The graph predicts which signals
    that change can reach. The waveforms say which signals actually changed. Any signal
    that changed but was not predicted means the graph is wrong.
    """
    start = bits_from_lines(d, lines)
    bits, cells, _ = reach(d, start, stop_at_registers=False)
    predicted = {n.split(".")[-1] for b in bits for n in d["names_of"].get(b, [])}

    before, after = read_vcd(before_vcd), read_vcd(after_vcd)
    shared = set(before) & set(after)
    changed = {n for n in shared if before[n] != after[n]}
    # keep only signals inside the design that the graph also knows about
    known = set(d["signals"]) | {n.split(".")[-1] for n in d["signals"]}
    def short(n):
        return n[len(prefix):].split(".")[-1] if n.startswith(prefix) else None
    changed_in_design = {short(n) for n in changed if short(n) and short(n) in known}
    missed = changed_in_design - predicted
    # A wire driven by an input comes from the testbench, not from the design, so no graph of the
    # design alone can predict it: the bug changed what the core asked for and the testbench answered
    # differently. Match on wires, not names, because designs alias inputs (dbg_mem_ready = mem_ready).
    input_bits = {b for bits in d["inputs"].values() for b in bits}
    from_outside = sorted(n for n in missed
                          if any(set(d["signals"][full]["bits"]) <= input_bits
                                 for full in d["signals"] if full.split(".")[-1] == n))
    inside = sorted(missed - set(from_outside))

    print("seeded from %d changed line(s): %d wires" % (len(lines), len(start)))
    print("graph predicts %d signals can be affected" % len(predicted))
    print("waveforms: %d signals dumped, %d changed, %d of those are design signals the graph knows"
          % (len(shared), len(changed), len(changed_in_design)))
    print("changed but not predicted: %d" % len(missed))
    for n in inside[:15]:
        print("  %s  (inside the design, the graph is wrong)" % n)
    if len(inside) > 15:
        print("  ... and %d more inside the design" % (len(inside) - 15))
    for n in from_outside:
        print("  %s  (an input, driven by the testbench, not by the design)" % n)
    print()
    print("SOUND: every signal the design itself drives was predicted." if not inside
          else "UNSOUND: the graph missed %d signal(s) the design drives." % len(inside))
    return len(inside)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    d, cmd = load(sys.argv[1]), sys.argv[2]
    if cmd == "stats":
        stats(d)
    elif cmd == "drivers":
        drivers(d, find(d, sys.argv[3]))
    elif cmd == "readers":
        readers(d, find(d, sys.argv[3]))
    elif cmd == "impact":
        impact(d, find(d, sys.argv[3]), as_tapeout="--tapeout" in sys.argv)
    elif cmd == "diff":
        diff(d, load(sys.argv[3]))
    elif cmd == "dot":
        dot(d, find(d, sys.argv[3]))
    elif cmd == "verify":
        at = sys.argv.index("--line")
        want = {int(x) for x in sys.argv[at + 1].split(",")}
        verify(d, sys.argv[3], sys.argv[4], want)
    else:
        sys.exit("unknown command: " + cmd)
