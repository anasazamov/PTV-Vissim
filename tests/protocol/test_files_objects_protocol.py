"""Protocol tests for the file and object tools (PLAN 10.2, 11 row M3).

The server runs as ``python -X utf8 -m vissim_mcp`` with the fake pywin32 modules. A network in a folder with
Uzbek and Cyrillic characters (``Samarqand_ko'cha``/``Самарканд``) is loaded, changed (dry run first), saved
under a new name and reloaded; JSON-RPC stays UTF-8 clean and every write lands in the audit log.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from protocol_helpers import ALL_TOOLS, finish, handshake

from stdio_client import StdioServer
from vissim_mcp.policy import NON_ASCII_WARNING

StartServer = Callable[..., StdioServer]
SAFE_ENV = {
    "VISSIM_EXE_PATTERN": r"^vissim_mcp_test_standin\d*\.exe$",
    "VISSIM_PID_DETECT_S": "0.3",
}


def test_non_ascii_network_round_trip(start_server: StartServer, isolated_env: dict[str, Path]) -> None:
    folder = isolated_env["projects"] / "Samarqand_ko'cha" / "Самарканд"
    folder.mkdir(parents=True)
    (folder / "tarmoq.inpx").write_text("<network/>", encoding="utf-8")
    server = start_server(env=SAFE_ENV)
    assert list(handshake(server)) == ALL_TOOLS
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True

    load = server.call_tool(
        "vissim_load_network", {"inpx_path": "Samarqand_ko'cha/Самарканд/tarmoq.inpx"}, timeout=30
    )["payload"]
    assert load["ok"] is True, load
    assert load["loaded_file"] == str((folder / "tarmoq.inpx").resolve())
    assert load["warnings"] == [NON_ASCII_WARNING] and load["counts"]["Links"] == 10

    dry = server.call_tool(
        "vissim_set_attributes",
        {"path": "Net.Links[1]", "values": {"Name": "Registon ko'chasi — Регистан"}, "dry_run": True},
    )["payload"]
    assert dry["old"] == {"Name": "Link 1"} and dry["new"] == {"Name": "Registon ko'chasi — Регистан"}
    written = server.call_tool(
        "vissim_set_attributes", {"path": "Net.Links[1]", "values": {"Name": "Registon ko'chasi — Регистан"}}
    )["payload"]
    assert written["new"] == {"Name": "Registon ko'chasi — Регистан"} and written["failed"] == []

    refused = server.call_tool("vissim_save_network", {"save_as": str(folder / "tarmoq.inpx")}, timeout=30)[
        "payload"
    ]
    assert refused["error"]["code"] == "INVALID_ARGUMENT"  # the loaded file needs overwrite_loaded
    saved = server.call_tool(
        "vissim_save_network", {"save_as": "Samarqand_ko'cha/Самарканд/yangi.inpx"}, timeout=30
    )["payload"]
    assert saved["ok"] is True and len(saved["sha256"]) == 64
    assert saved["saved_path"] == str((folder / "yangi.inpx").resolve())

    reload = server.call_tool("vissim_load_network", {"inpx_path": str(folder / "yangi.inpx")}, timeout=30)[
        "payload"
    ]
    assert reload["ok"] is True
    values = server.call_tool("vissim_get_attributes", {"path": "Net.Links[1]", "attributes": ["Name"]})[
        "payload"
    ]
    assert values["values"] == {"Name": "Registon ko'chasi — Регистан"}

    listing = server.call_tool(
        "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No", "Name"], "limit": 2}
    )["payload"]
    assert listing["rows"][0] == [1, "Registon ko'chasi — Регистан"] and listing["truncated"] is True
    describe = server.call_tool("vissim_describe", {"path": "Net.Links"})["payload"]
    assert any(member["name"] == "ItemByKey" for member in describe["members"])
    bulk = server.call_tool(
        "vissim_set_attribute_bulk",
        {
            "collection": "Net.Links",
            "attribute": "NumLanes",
            "items": [{"key": 2, "value": 2}],
            "dry_run": True,
        },
    )["payload"]
    assert bulk["ok"] is True and bulk["updated"] == 0

    health = server.call_tool("vissim_health")["payload"]
    assert health["loaded_file"] == str((folder / "yangi.inpx").resolve())
    finish(server)

    records = [
        json.loads(line)
        for file in sorted(isolated_env["audit"].glob("audit-*.jsonl"))
        for line in file.read_text(encoding="utf-8").splitlines()
    ]
    tools = [record["tool"] for record in records]
    assert tools == [
        "vissim_load_network",
        "vissim_set_attributes",
        "vissim_set_attributes",
        "vissim_save_network",
        "vissim_save_network",
        "vissim_load_network",
        "vissim_set_attribute_bulk",
    ]
    assert records[2]["new"] == {"Name": "Registon ko'chasi — Регистан"}
    assert records[4]["sha256"] == saved["sha256"]


def test_file_tools_fail_closed_without_allowed_directories(start_server: StartServer) -> None:
    server = start_server(env={**SAFE_ENV, "VISSIM_ALLOWED_DIRS": ""})
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    load = server.call_tool("vissim_load_network", {"inpx_path": "a.inpx"})["payload"]
    assert load["error"]["code"] == "PATH_DENIED"
    assert "no allowed directories configured" in load["error"]["message"]
    save = server.call_tool("vissim_save_network", {"save_as": "a.inpx"})["payload"]
    assert save["error"]["code"] == "PATH_DENIED"
    finish(server)
