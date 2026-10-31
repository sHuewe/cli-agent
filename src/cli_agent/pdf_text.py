"""Compatibility module and subprocess entry point for PDF extraction."""
from importlib import import_module as _import_module
import sys as _sys
if __name__ == "__main__":
    from cli_agent.context.pdf_text import _main
    raise SystemExit(_main(_sys.argv[1:]))
_sys.modules[__name__] = _import_module(".context.pdf_text", __package__)
