"""Tests for netbbs.net.nodeconfig (design doc round 28, issues #15/#1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from netbbs.net.nodeconfig import (
    ConfigError,
    LinkConfig,
    NodeConfig,
    TransportConfig,
    is_loopback_host,
    load_config,
)


# -- defaults / secure-by-default (issue #1) ---------------------------------


def test_default_config_disables_telnet_and_web():
    config = NodeConfig()
    assert config.telnet.enabled is False
    assert config.web.enabled is False


def test_default_config_enables_ssh():
    config = NodeConfig()
    assert config.ssh.enabled is True


def test_default_insecure_transports_bind_loopback_only():
    config = NodeConfig()
    assert is_loopback_host(config.telnet.host)
    assert is_loopback_host(config.web.host)


def test_no_transport_enabled_fails_validation():
    config = NodeConfig(
        telnet=TransportConfig(False, "127.0.0.1", 2323),
        ssh=TransportConfig(False, "0.0.0.0", 2222),
        web=TransportConfig(False, "127.0.0.1", 8080),
    )
    with pytest.raises(ConfigError, match="no transport is enabled"):
        config.validate()


# -- is_loopback_host ---------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.5.5", "::1", "localhost"])
def test_is_loopback_host_recognizes_loopback_addresses(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.1", "example.com", "203.0.113.5"])
def test_is_loopback_host_rejects_non_loopback(host):
    assert is_loopback_host(host) is False


# -- non-loopback warnings (issue #1) -----------------------------------------


def test_telnet_on_non_loopback_produces_warning():
    config = NodeConfig(telnet=TransportConfig(True, "0.0.0.0", 2323))
    warnings = config.describe_insecure_bindings()
    assert len(warnings) == 1
    assert "Telnet" in warnings[0]
    assert "0.0.0.0:2323" in warnings[0]


def test_telnet_on_loopback_produces_no_warning():
    config = NodeConfig(telnet=TransportConfig(True, "127.0.0.1", 2323))
    assert config.describe_insecure_bindings() == []


def test_web_on_non_loopback_produces_warning():
    config = NodeConfig(web=TransportConfig(True, "0.0.0.0", 8080))
    warnings = config.describe_insecure_bindings()
    assert len(warnings) == 1
    assert "web" in warnings[0]


def test_ssh_on_non_loopback_produces_no_warning():
    """SSH is encrypted regardless of bind address -- only the two
    plaintext transports (Telnet, plain web) warrant this warning."""
    config = NodeConfig(ssh=TransportConfig(True, "0.0.0.0", 2222))
    assert config.describe_insecure_bindings() == []


def test_disabled_insecure_transport_produces_no_warning():
    config = NodeConfig(telnet=TransportConfig(False, "0.0.0.0", 2323))
    assert config.describe_insecure_bindings() == []


# -- validation ----------------------------------------------------------------


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_invalid_port_fails_validation(port):
    config = NodeConfig(telnet=TransportConfig(True, "127.0.0.1", port))
    with pytest.raises(ConfigError, match="port"):
        config.validate()


def test_empty_host_fails_validation():
    config = NodeConfig(telnet=TransportConfig(True, "", 2323))
    with pytest.raises(ConfigError, match="host"):
        config.validate()


def test_nonpositive_throttle_value_fails_validation():
    from dataclasses import replace

    config = NodeConfig(throttle=replace(NodeConfig().throttle, max_attempts_per_connection=0))
    with pytest.raises(ConfigError, match="max_attempts_per_connection"):
        config.validate()


# -- CLI overrides ---------------------------------------------------------


def test_cli_can_enable_telnet_and_set_host():
    config = load_config(["--enable-telnet", "--telnet-host", "0.0.0.0", "--telnet-port", "2000"])
    assert config.telnet == TransportConfig(True, "0.0.0.0", 2000)


def test_cli_can_disable_ssh():
    config = load_config(["--disable-ssh", "--enable-telnet"])
    assert config.ssh.enabled is False


def test_cli_db_path_override():
    config = load_config(["--db", "custom.db", "--enable-telnet"])
    assert config.db_path == Path("custom.db")


def test_cli_identity_dir_and_node_name_override():
    config = load_config(
        ["--identity-dir", "custom-identity", "--node-name", "roanoke", "--enable-telnet"]
    )
    assert config.identity_dir == Path("custom-identity")
    assert config.node_name == "roanoke"


def test_default_identity_dir_and_node_name():
    config = load_config(["--enable-telnet"])
    assert config.identity_dir == Path("netbbs_identity")
    assert config.node_name == "netbbs-node"


def test_cli_missing_config_file_raises_config_error(tmp_path):
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigError, match="not found"):
        load_config(["--config", str(missing)])


# -- TOML file loading ---------------------------------------------------------


def test_toml_file_overrides_defaults(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text(
        """
        [database]
        path = "custom.db"

        [telnet]
        enabled = true
        host = "127.0.0.1"
        port = 9999

        [throttle]
        max_attempts_per_connection = 7
        """
    )
    config = load_config(["--config", str(config_file)])
    assert config.db_path == Path("custom.db")
    assert config.telnet == TransportConfig(True, "127.0.0.1", 9999)
    assert config.throttle.max_attempts_per_connection == 7


def test_cli_overrides_toml_file(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text(
        """
        [telnet]
        enabled = true
        host = "127.0.0.1"
        port = 1111
        """
    )
    config = load_config(["--config", str(config_file), "--telnet-port", "2222"])
    assert config.telnet.port == 2222
    assert config.telnet.host == "127.0.0.1"  # untouched by CLI, still from file


def test_toml_node_table_overrides_defaults(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text(
        """
        [node]
        identity_dir = "custom-identity"
        name = "roanoke"
        """
    )
    config = load_config(["--config", str(config_file)])
    assert config.identity_dir == Path("custom-identity")
    assert config.node_name == "roanoke"


def test_toml_unknown_node_key_raises_config_error(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("[node]\nbogus = 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(["--config", str(config_file)])


def test_toml_unknown_section_raises_config_error(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("[bogus]\nvalue = 1\n")
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(["--config", str(config_file)])


def test_toml_unknown_throttle_key_raises_config_error(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("[throttle]\nnot_a_real_setting = 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(["--config", str(config_file)])


def test_toml_malformed_syntax_raises_config_error(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("this is not [valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(["--config", str(config_file)])


def test_loaded_config_is_validated_end_to_end(tmp_path):
    """load_config itself must reject an invalid combination, not just
    expose validate() for the caller to remember to call."""
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("[ssh]\nenabled = false\n")
    with pytest.raises(ConfigError, match="no transport is enabled"):
        load_config(["--config", str(config_file), "--disable-telnet", "--disable-web"])


# -- Link config (design doc §11/§12, round 118) ------------------------------


def test_default_link_config_is_disabled_and_outgoing_only():
    config = NodeConfig()
    assert config.link.enabled is False
    assert config.link.outgoing_only is True


def test_link_alone_does_not_satisfy_no_transport_enabled_check():
    """Link is a machine-to-machine peer listener, not something a user
    connects to -- validate()'s "no transport is enabled" check (about
    interactive transports) must not be satisfied by Link alone."""
    config = NodeConfig(
        telnet=TransportConfig(False, "127.0.0.1", 2323),
        ssh=TransportConfig(False, "0.0.0.0", 2222),
        web=TransportConfig(False, "127.0.0.1", 8080),
        link=LinkConfig(enabled=True, host="127.0.0.1", port=7862),
    )
    with pytest.raises(ConfigError, match="no transport is enabled"):
        config.validate()


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_link_invalid_port_fails_validation(port):
    config = NodeConfig(link=LinkConfig(enabled=True, host="127.0.0.1", port=port))
    with pytest.raises(ConfigError, match="link.port"):
        config.validate()


def test_link_empty_host_fails_validation():
    config = NodeConfig(link=LinkConfig(enabled=True, host="", port=7862))
    with pytest.raises(ConfigError, match="link.host"):
        config.validate()


def test_link_disabled_skips_all_link_validation():
    """An invalid port/host on a *disabled* Link config must not block
    an otherwise-valid node from starting -- same "only validate what's
    actually enabled" shape telnet/ssh/web already follow."""
    config = NodeConfig(link=LinkConfig(enabled=False, host="", port=-1))
    config.validate()  # must not raise


def test_link_full_peer_without_advertised_host_fails_validation():
    config = NodeConfig(
        link=LinkConfig(enabled=True, host="0.0.0.0", port=7862, outgoing_only=False)
    )
    with pytest.raises(ConfigError, match="advertised_host"):
        config.validate()


def test_link_full_peer_with_invalid_advertised_port_fails_validation():
    config = NodeConfig(
        link=LinkConfig(
            enabled=True, host="0.0.0.0", port=7862, outgoing_only=False,
            advertised_host="203.0.113.5", advertised_port=99999,
        )
    )
    with pytest.raises(ConfigError, match="advertised_port"):
        config.validate()


def test_link_full_peer_with_valid_advertised_host_passes_validation():
    config = NodeConfig(
        link=LinkConfig(
            enabled=True, host="0.0.0.0", port=7862, outgoing_only=False,
            advertised_host="203.0.113.5",
        )
    )
    config.validate()  # must not raise


def test_link_outgoing_only_needs_no_advertised_host():
    config = NodeConfig(link=LinkConfig(enabled=True, host="127.0.0.1", port=7862, outgoing_only=True))
    config.validate()  # must not raise


def test_link_full_peer_produces_a_warning():
    config = NodeConfig(
        link=LinkConfig(
            enabled=True, host="0.0.0.0", port=7862, outgoing_only=False,
            advertised_host="203.0.113.5",
        )
    )
    warnings = config.describe_insecure_bindings()
    assert len(warnings) == 1
    assert "full peer" in warnings[0]
    assert "203.0.113.5" in warnings[0]


def test_link_outgoing_only_produces_no_warning():
    config = NodeConfig(link=LinkConfig(enabled=True, host="127.0.0.1", port=7862, outgoing_only=True))
    assert config.describe_insecure_bindings() == []


def test_link_disabled_produces_no_warning():
    config = NodeConfig(link=LinkConfig(enabled=False, host="0.0.0.0", port=7862, outgoing_only=False))
    assert config.describe_insecure_bindings() == []


def test_cli_can_enable_link_and_set_host_port():
    config = load_config(
        ["--enable-telnet", "--enable-link", "--link-host", "0.0.0.0", "--link-port", "7000"]
    )
    assert config.link.enabled is True
    assert config.link.host == "0.0.0.0"
    assert config.link.port == 7000


def test_cli_link_full_peer_flag_and_advertised_address():
    config = load_config(
        [
            "--enable-telnet", "--enable-link", "--link-full-peer",
            "--link-advertised-host", "203.0.113.5", "--link-advertised-port", "7001",
        ]
    )
    assert config.link.outgoing_only is False
    assert config.link.advertised_host == "203.0.113.5"
    assert config.link.advertised_port == 7001


def test_cli_link_outgoing_only_flag():
    config = load_config(["--enable-telnet", "--enable-link", "--link-outgoing-only"])
    assert config.link.outgoing_only is True


def test_toml_link_table_overrides_defaults(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text(
        """
        [telnet]
        enabled = true

        [link]
        enabled = true
        host = "0.0.0.0"
        port = 7862
        outgoing_only = false
        advertised_host = "203.0.113.5"
        advertised_port = 7001
        """
    )
    config = load_config(["--config", str(config_file)])
    assert config.link == LinkConfig(
        enabled=True, host="0.0.0.0", port=7862, outgoing_only=False,
        advertised_host="203.0.113.5", advertised_port=7001,
    )


def test_toml_unknown_link_key_raises_config_error(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text("[telnet]\nenabled = true\n\n[link]\nbogus = 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(["--config", str(config_file)])


def test_cli_overrides_toml_link_settings(tmp_path):
    config_file = tmp_path / "netbbs.toml"
    config_file.write_text(
        """
        [telnet]
        enabled = true

        [link]
        enabled = true
        port = 1111
        """
    )
    config = load_config(["--config", str(config_file), "--link-port", "2222"])
    assert config.link.port == 2222
    assert config.link.enabled is True  # untouched by CLI, still from file
