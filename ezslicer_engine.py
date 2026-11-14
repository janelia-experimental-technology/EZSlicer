"""Start EZSlicer's engine from any folder, with any Python 3.10+:

    python <path>/ezslicer_engine.py --engine          (HTTP engine; see docs/ENGINE.md)
    python <path>/ezslicer_engine.py bridge <command>  (JSON-lines bridge)

Unlike `python -m resin_slicer.engine`, this doesn't depend on the current folder being on
sys.path, so it also works with python.org's embeddable Python, whose ._pth file limits
imports to the folders it lists.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    if sys.argv[1:2] == ["bridge"]:
        from resin_slicer.electron_bridge import main as bridge_main

        raise SystemExit(bridge_main(sys.argv[2:]))
    from resin_slicer.engine import main

    raise SystemExit(main(sys.argv[1:]))
