import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot.checks import Refused, handle_app_command_error
from bot.gate import GuildLockedTree, gate_message
from bot.serversettings import ServerSettings
from tests.fakes import fake_interaction, fake_member, reply_text


def configured(conn, guild_id=111):
    server = ServerSettings(conn)
    server.configure(guild_id=guild_id, verified_role_id=2, officer_role_id=3)
    return server


def test_before_setup_only_the_setup_command_is_allowed(conn):
    server = ServerSettings(conn)
    assert gate_message(server, 111, "setup") is None
    assert "isn't set up yet" in gate_message(server, 111, "channel")
    assert "isn't set up yet" in gate_message(server, 111, None)      # a button press


def test_after_setup_the_configured_server_is_allowed(conn):
    server = configured(conn)
    assert gate_message(server, 111, "channel") is None
    assert gate_message(server, 111, None) is None


@pytest.mark.parametrize("root", ["channel", "setup", None])
def test_after_setup_other_servers_are_refused(conn, root):
    assert "different server" in gate_message(configured(conn), 999, root)


def interaction_for(conn, guild_id, command_name):
    interaction = fake_interaction(fake_member(1), MagicMock())
    interaction.guild_id = guild_id
    interaction.command = SimpleNamespace(qualified_name=command_name)
    interaction.client.app = SimpleNamespace(server=configured(conn))
    return interaction


def test_tree_check_raises_a_refusal_for_another_server(conn):
    interaction = interaction_for(conn, 999, "feed list")
    with pytest.raises(Refused, match="different server"):
        asyncio.run(GuildLockedTree.interaction_check(SimpleNamespace(), interaction))


def test_tree_check_allows_the_configured_server(conn):
    interaction = interaction_for(conn, 111, "feed list")
    assert asyncio.run(GuildLockedTree.interaction_check(SimpleNamespace(), interaction)) is True


def test_tree_check_reads_the_root_of_subcommands(conn):
    interaction = interaction_for(conn, 111, "setup roles")
    interaction.client.app = SimpleNamespace(server=ServerSettings(conn))      # not configured yet
    assert asyncio.run(GuildLockedTree.interaction_check(SimpleNamespace(), interaction)) is True


def test_error_handler_shows_the_refusal_text_not_the_officer_text():
    interaction = fake_interaction(fake_member(1), MagicMock())
    asyncio.run(handle_app_command_error(interaction, Refused("This bot is set up for a different server.")))
    assert reply_text(interaction) == "This bot is set up for a different server."
