# picorv32-depgraph

A small script that builds a dependency graph of a Verilog chip design and answers one question:
if I change this signal, what else can it affect?

I am teaching myself chip design. The advice I got was to read a real RISC-V core and learn how a
tape-out works, so I have been reading [PicoRV32](https://github.com/YosysHQ/picorv32). This is what
I built while doing that, because tracing signals by hand through 3,000 lines of Verilog got old fast.

It is one Python file, 218 lines, no dependencies. Yosys does the real work.

## The idea

A digital chip is a pile of registers with logic in between. On every clock edge each register takes
whatever the logic worked out from the registers' previous values. That is it. So the design is a
graph: wires are nodes, cells are the things that read some wires and write others, and "what does
this affect" is a walk forward through that graph.

Yosys reads the Verilog and writes a JSON netlist where every wire bit has a number, every cell lists
which bits it reads and writes, and everything carries the line of source it came from. My script
loads that JSON and walks it.

```
yosys -q -p "read_verilog picorv32.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json picorv32.json"
```

## What it does

```
$ python3 depgraph.py picorv32.json stats
top module: picorv32_axi
named signals: 240
cells: 919 (116 registers, 803 combinational)
chip outputs: 19
```

Follow one signal backwards to whatever writes it:

```
$ python3 depgraph.py picorv32.json drivers decoded_imm
picorv32_core.decoded_imm (32 bits, declared at picorv32.v:657) is written by 1 cell(s):
  register $dff at picorv32.v:858
      CLK  <- axi_adapter.clk, clk, picorv32_core.clk
      D    <- 32 unnamed wires
      Q    -> picorv32_core.decoded_imm
```

Or forwards, to everything that reads it:

```
$ python3 depgraph.py picorv32.json readers cpu_state
picorv32_core.cpu_state (8 bits, declared at picorv32.v:1181) is read by 109 cell(s):
  logic $eq at picorv32.v:1313
      A    <- picorv32_core.cpu_state
      B    <- constant 64
      Y    -> 1 unnamed wire
  logic $eq at picorv32.v:1878
      A    <- picorv32_core.cpu_state
      B    <- constant 1
      Y    -> 1 unnamed wire
  ... and 104 more
```

That one taught me something. The script knows nothing about PicoRV32, but those comparisons against
64 and 1 are the CPU's state machine. In the source, `cpu_state_fetch` is `8'b01000000`, which is 64,
and line 1313 turns out to be `if (cpu_state == cpu_state_fetch)`. Constant 1 is `cpu_state_ldmem`.
The graph found the state machine by itself and pointed at the exact line.

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

## Three things that surprised me

**On a CPU, almost everything reaches almost everything.** The program counter, the decoded immediate,
and the state machine all reach 893 of 919 cells and the same 15 outputs. So "what does this touch,
eventually" is close to useless as a way to decide what to re-test. The same-cycle number is the one
that varies and means something.

**The graph is of one configuration, not of the source.** PicoRV32 has parameters, and `ENABLE_IRQ`
defaults to 0. So `irq_pending` reaches 6 cells and zero chip outputs: the interrupt logic was compiled
out before I ever saw it. Change the parameter and the answer changes. A tool like this has to say
which configuration it is talking about.

**Names are not identity.** `diff` compares two versions of the design by signal name:

```
$ git -C picorv32 show 6d145b7^:picorv32.v > before.v && git -C picorv32 show 6d145b7:picorv32.v > after.v
$ for f in before after; do yosys -q -p "read_verilog $f.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json $f.json"; done
$ python3 depgraph.py before.json diff after.json
same name: 190   gone: 1   new: 1
  - decoded_imm_uj
  + decoded_imm_j
```

That is a real PicoRV32 commit (`6d145b7`) that renamed one signal and changed no logic at all. By name
it looks like something was deleted and something else appeared. Everything I know about the old signal,
every test that covered it, looks invalid, and none of that is true. I do not have a fix for this in the
script. Working out what "the same signal" means across two versions of a design seems to be the actual
hard part, and it is the thing I would most like to work on next.

## Run it

```
brew install yosys
git clone https://github.com/YosysHQ/picorv32
cd picorv32-depgraph
yosys -q -p "read_verilog ../picorv32/picorv32.v; hierarchy -top picorv32_axi; proc; flatten; opt_clean; write_json picorv32.json"
python3 depgraph.py picorv32.json stats
```

Python 3, nothing to install. Tested with Yosys 0.69 on macOS.

## Printing it as .tapeout

[Tapeout Labs](https://tapeoutlabs.com) publish a flat text format for hardware facts called
[tof](https://github.com/tapeout-labs/tof): one fact per line, and every line carries the source it
came from. I liked the idea enough to make the impact result print that way, which was about six lines
of code:

```
$ python3 depgraph.py picorv32.json impact decoded_imm --tapeout
tapeout 1
ip picorv32 top picorv32_axi
finding IMPACT-001 confirmed info "decoded_imm reaches 15 of 19 chip outputs" desc "same cycle 19 cells into 4 registers; eventually 893 of 919 cells" @ picorv32.v:657
```

It says `confirmed` because every number in it was computed by walking the netlist, not guessed.

## What it does not do

- Verilog only, whatever Yosys' built-in parser reads. SystemVerilog needs a Yosys built with the slang frontend.
- One configuration at a time, as above.
- Cells are Yosys' operators after `proc`, not gates and not statements of source. So a `$eq` is one node even if the line of Verilog it came from is much bigger.
- After `flatten` one wire can carry several names, which is why `CLK` above lists three. They are the same wire.
- `diff` compares by name only, for the reason in the section above.
- No tests. It is a reading tool, and I checked its answers by hand against the source.

## Notes

[NOTES.md](NOTES.md) is what I wrote down while reading PicoRV32, which is where all of this came from.
