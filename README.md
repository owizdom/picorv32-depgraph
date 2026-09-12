# picorv32-depgraph

A small script that builds a dependency graph of a Verilog chip design and answers one question:
if I change this signal, what else can it affect?

I am teaching myself chip design by reading [PicoRV32](https://github.com/YosysHQ/picorv32), a RISC-V
core in one file. Tracing signals by hand through 3,000 lines of Verilog got old, so I built this.
One Python file, 218 lines, no dependencies. Yosys does the real work.

## What we built

A chip is registers with logic in between, and on every clock edge each register takes whatever the
logic worked out from the previous values. So the design is a graph, and "what does this affect" is a
walk forward through it. Yosys reads the Verilog and writes a JSON netlist where every wire bit has a
number and every cell carries the source line it came from. The script loads that and walks it.

```
yosys -q -p "read_verilog picorv32.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json picorv32.json"
```

```
$ python3 depgraph.py picorv32.json stats
top module: picorv32_axi
named signals: 240
cells: 919 (116 registers, 803 combinational)
chip outputs: 19
```

Ask what reads a signal:

```
$ python3 depgraph.py picorv32.json readers cpu_state
picorv32_core.cpu_state (8 bits, declared at picorv32.v:1181) is read by 109 cell(s):
  logic $eq at picorv32.v:1313
      A    <- picorv32_core.cpu_state
      B    <- constant 64
      Y    -> 1 unnamed wire
  ... and 108 more
```

That one taught me something. The script knows nothing about PicoRV32, but comparing `cpu_state`
against 64 is the CPU's state machine: `cpu_state_fetch` is `8'b01000000`, which is 64, and line 1313
turns out to be `if (cpu_state == cpu_state_fetch)`. The graph found the state machine by itself and
pointed at the exact line.

And the whole downstream cone:

```
$ python3 depgraph.py picorv32.json impact reg_pc
picorv32_core.reg_pc (32 bits, declared at picorv32.v:176)
  same clock cycle: 25 cells, landing in 5 registers
  across clock cycles: 893 of 919 cells
  chip outputs it can affect: 15 of 19 (trap, mem_axi_awvalid, mem_axi_awaddr, ...)
```

Two numbers because there are two questions. Stop at the registers and you get what moves in this same
clock tick. Walk through them and you get what can move eventually, over many ticks.

It also goes backwards (`drivers`) and compares two versions of a design (`diff`).

## Why it matters

Knowledge about a design does not survive the design changing.

`diff` compares two versions by signal name, and on a real PicoRV32 commit (`6d145b7`) that renamed one
signal and changed no logic at all:

```
same name: 190   gone: 1   new: 1
  - decoded_imm_uj
  + decoded_imm_j
```

By name it looks like something was deleted and something else appeared. Everything I know about the old
signal, every test that covered it, looks invalid, and none of that is true. I do not have a fix in the
script. Working out what "the same signal" means across two versions of a design seems to be the actual
hard part, and it is the thing I would most like to work on next.

## Printing it as .tapeout

[Tapeout Labs](https://tapeoutlabs.com) publish a flat text format for hardware facts called
[tof](https://github.com/tapeout-labs/tof): one fact per line, and every line carries the source it came
from. I liked the idea enough to make the impact result print that way, which was about six lines of code:

```
$ python3 depgraph.py picorv32.json impact decoded_imm --tapeout
tapeout 1
ip picorv32 top picorv32_axi
finding IMPACT-001 confirmed info "decoded_imm reaches 15 of 19 chip outputs" desc "same cycle 19 cells into 4 registers; eventually 893 of 919 cells" @ picorv32.v:657
```

It says `confirmed` because every number in it was computed by walking the netlist, not guessed.

## Run it

```
brew install yosys
git clone https://github.com/YosysHQ/picorv32
yosys -q -p "read_verilog picorv32/picorv32.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json picorv32.json"
python3 depgraph.py picorv32.json stats
```

Python 3, nothing to install. Tested with Yosys 0.69 on macOS.

Limits worth knowing: Verilog only, one configuration at a time (`ENABLE_IRQ` defaults to 0, so the
interrupt logic is not in the graph at all), cells are Yosys operators after `proc` rather than gates,
and `diff` compares by name for the reason above. [NOTES.md](NOTES.md) is what I wrote down while
reading the core, including one thing I got wrong.
