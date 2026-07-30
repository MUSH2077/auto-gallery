from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "docs" / "api"
ASYNCAPI_SOURCE = BACKEND_ROOT / "app" / "contracts" / "asyncapi.yaml"
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app  # noqa: E402


def _openapi_bytes() -> bytes:
    app.openapi_schema = None
    payload = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True)
    return f"{payload}\n".encode()


def _asyncapi_bytes() -> bytes:
    return ASYNCAPI_SOURCE.read_bytes()


def _check_file(path: Path, expected: bytes) -> bool:
    if not path.exists():
        print(f"missing generated contract: {path.relative_to(REPOSITORY_ROOT)}")
        return False
    actual = path.read_bytes()
    if actual != expected:
        print(f"generated contract is stale: {path.relative_to(REPOSITORY_ROOT)}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Export deterministic public API contracts.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--check", action="store_true", help="Fail instead of writing when files drift.")
    args = parser.parse_args()

    output_root = args.output.resolve()
    files = {
        output_root / "openapi.json": _openapi_bytes(),
        output_root / "asyncapi.yaml": _asyncapi_bytes(),
    }

    if args.check:
        return 0 if all(_check_file(path, content) for path, content in files.items()) else 1

    output_root.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        path.write_bytes(content)
        print(f"wrote {path.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
