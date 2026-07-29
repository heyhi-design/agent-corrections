"""
Shared pytest setup: puts src/ on sys.path so test modules can `import corrections_cli`
and `import corrections_search` without the package needing an installable layout.

pytest imports conftest.py before collecting sibling test modules in this directory, so
the sys.path mutation below is in effect by the time test_*.py files run their imports.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
EXAMPLES = REPO_ROOT / "examples"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
