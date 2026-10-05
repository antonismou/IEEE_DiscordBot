from bot.serversettings import ServerSettings


def test_unconfigured_by_default(conn):
    server = ServerSettings(conn)
    assert server.guild_id is None and server.verified_role_id is None and server.officer_role_id is None
    assert server.configured is False


def test_configure_stores_and_reads_back(conn):
    server = ServerSettings(conn)
    server.configure(guild_id=111, verified_role_id=222, officer_role_id=333)
    assert (server.guild_id, server.verified_role_id, server.officer_role_id) == (111, 222, 333)
    assert server.configured is True
    assert ServerSettings(conn).guild_id == 111          # persisted, not cached in the object


def test_reconfigure_overwrites(conn):
    server = ServerSettings(conn)
    server.configure(guild_id=111, verified_role_id=222, officer_role_id=333)
    server.configure(guild_id=111, verified_role_id=444, officer_role_id=555)
    assert (server.verified_role_id, server.officer_role_id) == (444, 555)


def test_branch_roles_default_empty_and_round_trip(conn):
    server = ServerSettings(conn)
    assert server.branch_role_ids == {}
    server.configure_branches({"main": 1, "cs": 2, "ias": 3, "quantum": 4})
    assert ServerSettings(conn).branch_role_ids == {"main": 1, "cs": 2, "ias": 3, "quantum": 4}
