#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Print a single-file, in-memory copy of the CLI for running on a host that
has nothing installed, without writing anything there:

    python3 scripts/bundle.py | ssh <host> python3 - --direct --device /dev/ttyUSB0 probe

Only --direct makes sense this way (there is no daemon).
"""
from pathlib import Path

ORDER = ["__init__", "parse", "transport", "instrument", "control", "config", "probe", "cli"]
src = Path(__file__).resolve().parent.parent / "src" / "kiprim"
sources = {name: (src / f"{name}.py").read_text() for name in ORDER}
print("import sys, types")
print(f"_SOURCES = {sources!r}")
print('''
for _name in %r:
    _full = "kiprim" if _name == "__init__" else "kiprim." + _name
    _mod = types.ModuleType(_full)
    _mod.__package__ = "kiprim"
    if _name == "__init__":
        _mod.__path__ = []
    sys.modules[_full] = _mod
    if _name != "__init__":
        setattr(sys.modules["kiprim"], _name, _mod)
    exec(compile(_SOURCES[_name], f"<kiprim/{_name}.py>", "exec"), _mod.__dict__)
sys.argv[0] = "kiprim"
sys.exit(sys.modules["kiprim.cli"].main())
''' % (ORDER,))
