"""One-time production bootstrap after Alembic migrations have been applied."""

from app.seed import seed_database


if __name__ == "__main__":
    seed_database()
    print("LedgerCore bootstrap completed.")
