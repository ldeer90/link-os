#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guest_post_prospecting.universe import export_universe


def main() -> None:
    print(json.dumps(export_universe(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
