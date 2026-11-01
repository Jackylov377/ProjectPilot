"""Inventory one explicit application data root without starting the application."""

import argparse
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workspace.inventory import (  # noqa: E402
    REASONS, digest, file_manifest, inventory, stable_snapshot, write_inventory,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "runs/web")
    parser.add_argument("--output", type=Path, help="New output directory; must not exist")
    stability = parser.add_mutually_exclusive_group()
    stability.add_argument("--snapshot", action="store_true", help="Verify and read a temporary byte-identical copy")
    stability.add_argument("--stable-source", help="Describe how writes were paused or a stable copy was obtained")
    args = parser.parse_args(argv)
    source = args.data_root.resolve()
    output = (args.output or ROOT / "output/ui-change/phase1" / uuid4().hex).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("Output must be a new directory separate from the input")
    try:
        if args.snapshot:
            with tempfile.TemporaryDirectory(prefix="projectpilot-inventory-") as temporary:
                snapshot = Path(temporary) / "snapshot"
                proof = stable_snapshot(source, snapshot)
                result = inventory(snapshot, original_root=source,
                                   stable_source="Verified byte-identical snapshot; no active receipts")
                result["snapshot_proof"] = proof
                after, errors = file_manifest(source)
                if errors or digest(after) != proof["manifest_sha256"]:
                    result["scan_status"] = "partial"
                    result["incomplete_ranges"].append({"path": str(source), "code": "origin_changed_after_snapshot"})
                    for entry in result["entries"]:
                        if entry["classification"] == "candidate":
                            entry["classification"] = "needs_confirmation"
                            entry["reason_codes"].append("scan_unstable")
                            entry["reasons"].append({"code": "scan_unstable", "description": REASONS["scan_unstable"]})
                            result["counts"]["candidate"] -= 1
                            result["counts"]["needs_confirmation"] += 1
                    result["stability"]["origin_unchanged_after_scan"] = False
                else:
                    result["stability"]["origin_unchanged_after_scan"] = True
        else:
            result = inventory(source, stable_source=args.stable_source)
        write_inventory(result, output)
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "Input/output unavailable"
        print(f"Inventory failed ({type(exc).__name__}): {reason}; no complete inventory claimed.", file=sys.stderr)
        return 1
    print(f"Inventory: {result['scan_status']}; counts: {result['counts']}")
    print(f"Output: {output}")
    return 0 if result["scan_status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
