import diagnostics


def test_diagnostics_hides_keys():
    entry = type("E", (), {"data": {
        "adapter": "hci0",
        "source_id": 0x7D00,
        "beacon_key": "aa" * 16,
        "bulbs": [{"mac": "AA:BB:CC:DD:EE:FF", "node_id": 16,
                    "local_key": "0123456789ABCDEF", "name": "Office"}],
    }})()
    import asyncio
    data = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    text = str(data)
    assert "aa" * 16 not in text
    assert "0123456789ABCDEF" not in text
    assert data["adapter"] == "hci0"
    assert data["bulbs"][0]["node_id"] == 16
