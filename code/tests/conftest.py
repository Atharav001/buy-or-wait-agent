import sys
from pathlib import Path

code_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(code_root))

DATASET = code_root.parent / "dataset"