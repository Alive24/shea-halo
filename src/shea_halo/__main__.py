from __future__ import annotations

import asyncio
import sys

from shea_halo.service import run_worker


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit(
            "Shea Halo accepts no action arguments; control research through GitHub Issues."
        )
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
