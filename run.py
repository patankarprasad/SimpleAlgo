import os
import sys

if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
else:
    _exe_dir = os.path.dirname(os.path.abspath(__file__))

if sys.path[0] != _exe_dir:
    sys.path.insert(0, _exe_dir)

import runpy
runpy.run_module("main", run_name="__main__", alter_sys=True)
