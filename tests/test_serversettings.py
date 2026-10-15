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


def test_branch_key_is_a_safe_slug():
    from bot.serversettings import branch_key
    assert branch_key("  Antennas & Propagation Society! ") == "antennas-propagation-society"
    assert branch_key("a_b") == "a-b" and branch_key("???") == ""


def test_dynamic_branches_keep_order_and_do_not_mix_with_pin_or_description_rows(conn):
    server = ServerSettings(conn)
    server.add_branch("zeta", "Zeta", 1)
    server.add_branch("alpha", "Alpha", 2)
    server.set_branch_pin("zeta", "123456")
    server.set_branch_description("zeta", "text")
    assert server.branch_role_ids == {"zeta": 1, "alpha": 2}
    assert server.branch_labels == {"zeta": "Zeta", "alpha": "Alpha"}
    server.remove_branch("zeta")
    assert server.branch_role_ids == {"alpha": 2} and server.branch_pin_keys() == set()
