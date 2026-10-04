import pytest

from bot.db import connect
from bot.members import MemberRepo


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "data" / "bot.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def members(conn):
    return MemberRepo(conn)
