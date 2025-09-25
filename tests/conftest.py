import pathlib
import sys

# Ensure the src directory is on sys.path for tests without installation.
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

