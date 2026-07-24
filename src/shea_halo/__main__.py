from __future__ import annotations

import asyncio
import sys

from shea_halo.service import run_worker

if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit(
            "Shea Halo accepts no action arguments; control research through GitHub Issues."
        )
    asyncio.run(run_worker())
