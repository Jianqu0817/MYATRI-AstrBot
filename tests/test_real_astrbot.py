import asyncio
from contextlib import closing
import os
import sqlite3

import pytest
from aiohttp.test_utils import TestServer

from conftest import PROJECT
from myatri_bridge.server import create_app


async def test_pinned_astrbot_plugin_against_real_bridge(bridge, tmp_path):
    python = PROJECT / "upstream/AstrBot/.venv/Scripts/python.exe"
    if not python.exists():
        pytest.skip(
            "Install the pinned AstrBot environment to run its actual API integration"
        )
    token = "local-fixture-" + "b" * 48
    token_file = tmp_path / "bridge-token.txt"
    token_file.write_text(token, encoding="ascii")
    env = {**os.environ, "ASTRBOT_ROOT": str(tmp_path / "astrbot"), "PYTHONUTF8": "1"}
    async with TestServer(create_app(bridge, token)) as server:
        process = await asyncio.create_subprocess_exec(
            str(python),
            str(PROJECT / "scripts/smoke_plugin.py"),
            "--url",
            str(server.make_url("/")).rstrip("/"),
            "--token-file",
            str(token_file),
            cwd=tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        assert process.returncode == 0, (stdout + stderr).decode(
            "utf-8", errors="replace"
        )
    with closing(sqlite3.connect(bridge.path)) as db:
        statuses = sorted(row[0] for row in db.execute("SELECT status FROM deliveries"))
    assert statuses == ["sent", "unknown"]
    assert (
        len(bridge.test_providers) == 1 and len(bridge.test_providers[0].requests) == 2
    )
