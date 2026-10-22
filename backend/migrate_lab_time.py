"""Opt-in timing migration for the configured application database."""
import argparse


def main():
    parser = argparse.ArgumentParser(description="Add learner session timing without resetting data")
    parser.add_argument('--apply-lab-time-schema', action='store_true', required=True)
    parser.parse_args()
    from app.database import engine
    from app.lab_time_migration import migrate
    try:
        with engine.begin() as connection:
            changed = migrate(connection)
        print('Lab time schema applied.' if changed else 'Lab time schema already current.')
    except Exception:
        print('Lab time migration refused or failed; inspect schema compatibility privately. No database reset performed.')
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
