"""Shared fixtures: isolated temp data dirs and in-process DICOM nodes."""

from __future__ import annotations

import time

import pytest

from server import config
from server.store import InstanceStore


@pytest.fixture()
def temp_config(tmp_path, monkeypatch):
    """Point all storage paths at a temp directory."""
    data = tmp_path / "data"
    received = data / "received"
    quarantine = data / "quarantine"
    exports = data / "exports"
    for p in (received, quarantine, exports):
        p.mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RECEIVED_DIR", received)
    monkeypatch.setattr(config, "QUARANTINE_DIR", quarantine)
    monkeypatch.setattr(config, "EXPORT_DIR", exports)
    monkeypatch.setattr(config, "INDEX_PATH", data / "index.json")
    return config


@pytest.fixture()
def store(temp_config):
    return InstanceStore()


@pytest.fixture()
def dicom_nodes(temp_config, monkeypatch):
    """Demo SCP + workstation receiver on test ports, both in-process."""
    from server.demo_scp import make_server
    from server.receiver import Receiver

    monkeypatch.setattr(config, "DEMO_PORT", 21112)
    monkeypatch.setattr(config, "WS_CSTORE_PORT", 21113)
    monkeypatch.setitem(
        config.MOVE_DESTINATIONS, config.WS_AE_TITLE, (config.WS_CSTORE_HOST, 21113)
    )

    ae, handlers = make_server()
    server = ae.start_server(
        (config.DEMO_HOST, 21112), block=False, evt_handlers=handlers
    )
    st = InstanceStore()
    recv = Receiver(st)
    recv.start()
    time.sleep(0.2)
    yield st, recv
    recv.shutdown()
    server.shutdown()
