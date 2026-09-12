# Notes from reading PicoRV32

Kept while working through [picorv32.v](https://github.com/YosysHQ/picorv32), 3,049 lines, one file,
RV32IMC, by Claire Wolf. Corrections welcome, I am new at this.

## The shape of it

Eight modules in the file. `picorv32` is the core. `picorv32_axi` wraps it and speaks AXI4-Lite,
`picorv32_wb` speaks Wishbone, and `picorv32_axi_adapter` sits between the core's own memory interface
and AXI. Then `picorv32_regs` for the register file, and three optional co-processors reached through
a side channel called PCPI: `picorv32_pcpi_mul`, `picorv32_pcpi_fast_mul`, `picorv32_pcpi_div`.

Almost everything is a parameter. `ENABLE_MUL`, `ENABLE_DIV`, `COMPRESSED_ISA`, `ENABLE_IRQ`,
`BARREL_SHIFTER`, `TWO_CYCLE_ALU`. The same file is a different chip depending on what you set, which
is why my script keeps saying "one configuration". With the defaults, the multiplier, the divider,
compressed instructions and interrupts are all compiled out.

## It is not pipelined

I expected stages. It has a state machine instead, `cpu_state`, one-hot in 8 bits at line 1181:

```
cpu_state_trap   = 8'b10000000   (128)
cpu_state_fetch  = 8'b01000000   (64)
cpu_state_ld_rs1 = 8'b00100000   (32)
cpu_state_ld_rs2 = 8'b00010000   (16)
cpu_state_exec   = 8'b00001000   (8)
cpu_state_shift  = 8'b00000100   (4)
cpu_state_stmem  = 8'b00000010   (2)
cpu_state_ldmem  = 8'b00000001   (1)
```

One-hot means one bit set per state, so a comparison is cheap. An instruction walks fetch, load its
operands, execute, maybe a memory or shift state, then back to fetch. Several clock cycles per
instruction. That is why it is small and not fast, and it is why you can actually follow it.

## The memory interface

Four signals do the work: `mem_valid` from the core, `mem_ready` back from memory, `mem_addr`,
and `mem_wdata` with `mem_wstrb` for writes. Core raises valid, waits for ready. The AXI adapter turns
that handshake into the five AXI channels, and reading it taught me more about buses than the spec did.

I got one thing wrong here at first. My script showed `mem_addr`, `mem_axi_araddr` and `mem_axi_awaddr`
all coming out of a single register at line 565, and I assumed the adapter was doing something clever to
share it. It is not. Line 565 is in the core, the address register is assigned at line 575, and the
adapter is two plain wires:

```
assign mem_axi_awaddr = mem_addr;
assign mem_axi_araddr = mem_addr;
```

The AXI read address channel and the write address channel are the same wire. The graph was right and my
explanation was wrong, which I only found by going back to the source.

## Things that confused me at first

**`<=` versus `=`.** Inside an `always @(posedge clk)` block, `<=` means every assignment lands at the
same instant, at the clock edge. So `a <= b; b <= a;` swaps them. With `=` it would not. This is the
thing that makes hardware parallel rather than sequential, and it took a while to stop reading these
blocks like a program.

**`reg` does not mean register.** It is a Verilog storage class. A `reg` assigned inside a clocked block
becomes flip-flops, but a `reg` assigned in a combinational block is just wires. The word is a trap.

**Everything happens at once.** There are around 30 `always` blocks in the core and all of them are live
on every clock edge. There is no line that runs "next".

## Tape-out, in as far as I understand it now

The RTL is not the chip. Synthesis turns it into a netlist of gates from the foundry's cell library.
Placement gives every gate a position, routing draws the wires, then geometry checks, then a GDSII file
goes to the foundry, and that moment is the tape-out. Masks get made, wafers get processed, and you
cannot change anything afterwards without paying for new masks.

Which explains why verification is most of the work. Surveys from Siemens and the Wilson Research Group
put a design engineer's own time at about half verification, with roughly as many verification engineers
as designers, and only 14% of chip projects worked on first silicon in 2024. You cannot patch a chip,
so every bug has to be found before that one irreversible moment.

## What I want to understand next

- How a coverage database ties to the RTL, so "which tests touched this line" becomes a real edge in a
  graph like mine rather than something I invented.
- Assertions. They look like the most useful thing in the language and I have only written toy ones.
- What "the same signal" means across two versions of a design, which is the open question at the end of
  the README.
