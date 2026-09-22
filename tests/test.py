from pathlib import Path
import sys

p = Path(".").resolve()
print(p.parent)
print(sys.path)