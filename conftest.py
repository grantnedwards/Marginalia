"""Make `import marginalia` resolve to THIS tree, not a sibling checkout.

The venv is shared with ../marginalia; without this, an editable install there
would shadow this package and the tests would silently exercise the wrong code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
