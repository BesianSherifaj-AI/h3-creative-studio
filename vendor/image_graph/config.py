"""Optional local model search locations for the upstream graph catalogue."""
import os
from pathlib import Path

MODEL_ROOTS = [Path(p).expanduser() for p in os.environ.get("H3_MODEL_ROOTS", "").split(os.pathsep) if p]
