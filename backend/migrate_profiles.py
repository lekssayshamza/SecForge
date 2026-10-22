"""Explicit opt-in additive migration for the configured application database."""
import argparse


def main():
    parser = argparse.ArgumentParser(description='Add public profile fields and case-insensitive username uniqueness')
    parser.add_argument('--apply-profile-schema', action='store_true', required=True)
    parser.parse_args()
    from app.database import engine
    from app.profile_migration import migrate
    try:
        with engine.begin() as connection:
            changed = migrate(connection)
        print('Profile schema migration applied.' if changed else 'Profile schema already current.')
    except Exception:
        print('Profile migration refused or failed; transaction rolled back. Check schema compatibility and case-insensitive username collisions privately.')
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
