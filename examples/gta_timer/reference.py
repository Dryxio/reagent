"""Run a GTA SA timer leaf function from a user-owned PE image under Unicorn.

Usage: python reference.py /path/to/gta_sa_compact1.0.exe 0x561a40
Input/output: one JSON integer. Only the selected leaf and its mapped data run.
"""

from __future__ import annotations

import json
import struct
import sys

import pefile
from unicorn import UC_ARCH_X86, UC_MODE_32, Uc
from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_EIP, UC_X86_REG_ESP


def main() -> None:
    executable, address = sys.argv[1], int(sys.argv[2], 0)
    if address not in {0x561A40, 0x561AF0, 0x561B00}:
        raise ValueError("Only the three documented timer leaves are supported")
    value = json.load(sys.stdin)
    pe = pefile.PE(executable)
    base = pe.OPTIONAL_HEADER.ImageBase
    image = pe.get_memory_mapped_image()
    vm = Uc(UC_ARCH_X86, UC_MODE_32)
    size = (max(len(image), pe.OPTIONAL_HEADER.SizeOfImage) + 4095) & ~4095
    vm.mem_map(base, size)
    vm.mem_write(base, image)
    stack, stop = 0x3000000, 0x3100000
    vm.mem_map(stack, 0x10000)
    vm.mem_map(stop, 0x1000)
    vm.reg_write(UC_X86_REG_ESP, stack + 0x8000)
    vm.mem_write(stack + 0x8000, struct.pack("<I", stop))
    vm.mem_write(0xB7CB2C, struct.pack("<I", int(value) & 0xFFFFFFFF))
    vm.mem_write(0xB7CB49, bytes([int(value) & 1]))
    vm.emu_start(address, stop, timeout=1_000_000, count=100)
    if vm.reg_read(UC_X86_REG_EIP) != stop:
        raise RuntimeError("Reference did not return within its execution budget")
    result = vm.reg_read(UC_X86_REG_EAX) if address == 0x561A40 else vm.mem_read(0xB7CB49, 1)[0]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
