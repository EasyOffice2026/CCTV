from alembic import command


def test_models_match_migrations(owner_engine, alembic_config):
    """Fails when a model changed without a new migration (alembic revision --autogenerate)."""
    command.check(alembic_config)


def test_downgrade_then_upgrade(owner_engine, alembic_config):
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
