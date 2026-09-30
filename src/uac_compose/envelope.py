"""One UAC object plus what is needed to route it.

The object itself stays a plain dict: the engine is template-driven, so a
model per UAC type would be maintenance for no gain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ObjectEnvelope:
    type: str
    """The registry type (`taskUnix`, `credential`...), never a credential's own `type`."""
    name: str
    obj: dict[str, Any]
    """The object as exported, untouched."""
    release: str
    kind: str
    export_table: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        blob = json.dumps(self.obj, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()
