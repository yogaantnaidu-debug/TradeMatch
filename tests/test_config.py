"""DATABASE_URL resolution order: environment, then .env, then default."""
from tradesync import config


def test_env_file_is_parsed(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\nPOSTGRES_PASSWORD=s3cret\nDATABASE_URL="postgresql://u:p@h:1/d"\n\n')
    assert config.read_env_file(f) == {"POSTGRES_PASSWORD": "s3cret", "DATABASE_URL": "postgresql://u:p@h:1/d"}
    assert config.read_env_file(tmp_path / "missing") == {}


def test_environment_wins_over_env_file(monkeypatch):
    monkeypatch.setattr(config, "read_env_file", lambda: {"DATABASE_URL": "from-file"})
    monkeypatch.setenv("DATABASE_URL", "from-env")
    assert config.database_url() == "from-env"
    monkeypatch.delenv("DATABASE_URL")
    assert config.database_url() == "from-file"
    monkeypatch.setattr(config, "read_env_file", lambda: {})
    assert config.database_url() == config.DEFAULT_DATABASE_URL
