"""Tests for the Lymow binary sensor platform."""

from types import SimpleNamespace

import pytest

from custom_components.lymow.binary_sensor import BINARY_SENSORS


def _value_fn(key: str):
    return next(
        description.value_fn
        for description in BINARY_SENSORS
        if description.key == key
    )


@pytest.mark.parametrize(
    ("payload", "wifi_connected", "lte_connected"),
    [
        ({"netDetailInfo": {"currentNet": 1}}, True, False),
        ({"netDetailInfo": {"currentNet": 2}}, False, True),
        (
            {"netDetailInfo": SimpleNamespace(currentNet=1)},
            True,
            False,
        ),
        (
            {"netDetailInfo": SimpleNamespace(currentNet=2)},
            False,
            True,
        ),
        ({"currentNet": 1}, True, False),
        ({"currentNet": 2}, False, True),
    ],
)
def test_network_connectivity_payloads(
    payload: dict,
    wifi_connected: bool,
    lte_connected: bool,
) -> None:
    """Support dict, protobuf-like, and flat network payloads."""
    assert _value_fn("wifi_connected")(payload) is wifi_connected
    assert _value_fn("lte_connected")(payload) is lte_connected
