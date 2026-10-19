import pytest

from seqledger import db as odb


@pytest.fixture
def fresh_db(tmp_path):
    conn = odb.connect(str(tmp_path / "cat.db"))
    odb.init_db(conn)
    return conn
