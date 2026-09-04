"""Compile a candidate in a minimal, explicit timer ABI adapter.

This verifies the selected function and its observable field, not a full GTA build.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from re_agent.config.schema import ProjectProfile
from re_agent.parity.source_indexer import SourceIndexer


def main() -> None:
    file, output, method = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    indexer = SourceIndexer(file.parent, ProjectProfile(source_extensions=[".cpp"]))
    matches = [m for m in indexer.find_all("CTimer", method) if Path(m.path) == file]
    if len(matches) != 1:
        raise ValueError("Expected exactly one target definition in candidate overlay")
    result_type = "uint32" if method == "GetCyclesPerMillisecond" else "void"
    observe = "CTimer::GetCyclesPerMillisecond()" if result_type == "uint32" else "unsigned(CTimer::m_UserPause)"
    invoke = "" if result_type == "uint32" else f"CTimer::{method}();"
    code = """#include <cstdint>
#include <iostream>
using uint32 = std::uint32_t;
class CTimer { public: static uint32 m_snTimerDivider; static bool m_UserPause;
static uint32 GetCyclesPerMillisecond(); static void StartUserPause(); static void EndUserPause(); };
uint32 CTimer::m_snTimerDivider=0; bool CTimer::m_UserPause=false;
"""
    code += f"{result_type} CTimer::{method}() {matches[0].body}\n"
    code += (
        "int main() { unsigned long long input; std::cin >> input; "
        "CTimer::m_snTimerDivider=uint32(input); CTimer::m_UserPause=bool(input & 1); "
        + invoke
        + f" std::cout << {observe}; }}\n"
    )
    translation = output.with_suffix(".cpp")
    translation.write_text(code, encoding="utf-8")
    subprocess.run(["c++", "-std=c++17", str(translation), "-o", str(output)], check=True)


if __name__ == "__main__":
    main()
