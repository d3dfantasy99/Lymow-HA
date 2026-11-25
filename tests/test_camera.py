"""Tests for the Lymow camera platform."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.lymow import camera as camera_module
from custom_components.lymow.camera import LymowRTSPCamera, _get_robot_ip
from custom_components.lymow.const import F_IP_ADDRESS, F_NET_DETAIL


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({F_IP_ADDRESS: "192.0.2.1"}, "192.0.2.1"),
        ({F_NET_DETAIL: {"wifiIp": "192.0.2.2"}}, "192.0.2.2"),
        (
            {F_NET_DETAIL: SimpleNamespace(wifiIp="192.0.2.3")},
            "192.0.2.3",
        ),
        ({"rest_ip_address": "192.0.2.4"}, "192.0.2.4"),
        ({F_NET_DETAIL: None}, None),
    ],
)
def test_get_robot_ip(data: dict, expected: str | None) -> None:
    """Resolve the mower IP from dict and protobuf-like payloads."""
    assert _get_robot_ip(data) == expected


@pytest.mark.asyncio
async def test_hls_proxy_updates_existing_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point an already-created HA stream at the local HLS proxy."""
    coordinator = MagicMock()
    coordinator.thing_name = "test-mower"
    coordinator.data = {F_NET_DETAIL: SimpleNamespace(wifiIp="192.0.2.5")}
    entity = LymowRTSPCamera(coordinator)
    entity.stream = MagicMock()

    http_server = MagicMock()
    http_thread = MagicMock()
    proxy_process = SimpleNamespace(pid=1234)

    monkeypatch.setattr(camera_module.tempfile, "mkdtemp", lambda **_: "/tmp/hls")
    monkeypatch.setattr(camera_module, "_find_free_port", lambda: 12345)
    monkeypatch.setattr(camera_module, "HTTPServer", MagicMock(return_value=http_server))
    monkeypatch.setattr(
        camera_module.threading,
        "Thread",
        MagicMock(return_value=http_thread),
    )
    monkeypatch.setattr(
        camera_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proxy_process),
    )

    await entity._start_hls_proxy()

    entity.stream.update_source.assert_called_once_with(
        "http://127.0.0.1:12345/stream.m3u8"
    )
