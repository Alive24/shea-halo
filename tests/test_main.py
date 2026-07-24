from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from shea_halo.__main__ import main


def test_ctrl_c_exits_without_an_uncaught_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(coroutine: Coroutine[Any, Any, None]) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("shea_halo.__main__.sys.argv", ["python -m shea_halo"])
    monkeypatch.setattr("shea_halo.__main__.asyncio.run", interrupt)

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 130
