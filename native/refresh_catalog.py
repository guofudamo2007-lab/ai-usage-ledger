"""Refresh build-time metadata from the companion edition. No ledger data is accessed."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ledger import appearance, core, pricing, subscriptions

bundle = dict(
    entries=core.ENTRIES, presets=appearance.PRESETS, fonts=appearance.FONTS,
    appearance=appearance.checked({}), rates=pricing.CATALOG, version=pricing.CATALOG_VERSION,
    provider=pricing._PROVIDER_DOT_RE.pattern, enum=pricing._MODEL_ENUM_RE.pattern,
    services=subscriptions.SERVICES,
)
Path(__file__).with_name("catalog.json").write_bytes(
    (json.dumps(bundle, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
)

