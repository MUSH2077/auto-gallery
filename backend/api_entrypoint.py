"""Launch the API through Uvicorn's native worker supervisor."""

from __future__ import annotations

import sys

from app.api_runtime import prepare_api_environment


def main() -> None:
    workers = prepare_api_environment()
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=workers)


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(f"[api_entrypoint] configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
