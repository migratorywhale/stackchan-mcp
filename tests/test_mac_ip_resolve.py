import logging

from mcp_server.stackchan_config import resolve_mac_ip


def test_explicit_value_is_kept():
    assert resolve_mac_ip("10.0.0.5", "127.0.0.1") == "10.0.0.5"


def test_auto_detects_loopback_route():
    # Route to loopback resolves to a loopback address.
    assert resolve_mac_ip("auto", "127.0.0.1").startswith("127.")


def test_empty_behaves_like_auto():
    assert resolve_mac_ip("", "127.0.0.1").startswith("127.")


def test_auto_unroutable_falls_back(caplog):
    # RFC 5737 TEST-NET: connect() may still "succeed" on some hosts (UDP has no
    # handshake), so only assert we return a non-empty IPv4-looking string.
    with caplog.at_level(logging.WARNING):
        ip = resolve_mac_ip("auto", "192.0.2.1")
    assert ip.count(".") == 3


def test_stale_explicit_warns(caplog):
    with caplog.at_level(logging.WARNING):
        out = resolve_mac_ip("203.0.113.9", "127.0.0.1")
    assert out == "203.0.113.9"
    assert any("MAC_IP=203.0.113.9" in r.message for r in caplog.records)
