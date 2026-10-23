"""Entry point of the optional person-mask service (packages/services/person_mask/server.py).

    python -m packages.services.person_mask.main --host 127.0.0.1 --port 8013

Self-contained: imports neither packages/config.py nor any database client, so its image carries only this
directory (Dockerfile next to this file).
"""
import argparse
import logging

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Person segmentation for dynamic-object removal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8013)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from packages.services.person_mask.server import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
