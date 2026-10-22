"""Explicit additive migration for the configured application database. No operation on import."""
import argparse


def main():
    parser = argparse.ArgumentParser(description="Add challenge-learning tables; existing rooms/data are preserved")
    parser.add_argument("--apply-challenge-schema", action="store_true", required=True)
    args = parser.parse_args()
    if not args.apply_challenge_schema: parser.error("Explicit opt-in required")
    from app.database import engine
    from app.challenge_migration import migrate
    try:
        with engine.begin() as connection:
            changed = migrate(connection)
        print("Challenge schema migration applied." if changed else "Challenge schema already current.")
    except Exception:
        # DSNs and raw SQL exceptions must not appear in operator output.
        print("Challenge schema migration failed; transaction rolled back. Inspect schema compatibility privately.")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
