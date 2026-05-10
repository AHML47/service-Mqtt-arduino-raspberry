"""
Entry point: python3 -m service [--config path]
"""

import argparse
import logging
import sys

from .config import load_config
from .service import ArduinoBridgeService


def main():
    parser = argparse.ArgumentParser(description="Arduino Bridge Service")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Set up logging
    log_level = config.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)-22s] %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    service = ArduinoBridgeService(config)
    service.run()


if __name__ == "__main__":
    main()
