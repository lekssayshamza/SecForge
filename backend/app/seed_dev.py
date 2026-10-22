#!/usr/bin/env python3
"""
Development seed script for SecForge.

Creates a known creator account for local development.
This script is idempotent - running it multiple times will not create duplicate users.

Usage:
    cd backend
    python -m app.seed_dev
"""

import os
import sys

# Ensure we use development environment
os.environ.setdefault("TERRAFORM_DRY_RUN", "True")

# Add project root to path
sys.path.insert(0, "/home/hamza/secforge/backend")

from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.security import hash_password


DEV_CREATOR_USERNAME = "creator"
DEV_CREATOR_EMAIL = "creator@secforge.local"
DEV_CREATOR_PASSWORD = "creator123"  # Development-only password, clearly documented
DEV_CREATOR_ROLE = "creator"


def seed_dev_user():
    """Create or verify the development creator account."""
    if settings.production:
        raise RuntimeError("Development seed is disabled in production")
    db = SessionLocal()
    try:
        # Check if user already exists
        existing = db.query(User).filter(User.username == DEV_CREATOR_USERNAME).first()

        if existing:
            print(f"✓ Development creator account already exists: {DEV_CREATOR_USERNAME}")
            print(f"  Email: {existing.email}")
            print(f"  Role: {existing.role}")
            print(f"  ID: {existing.id}")
            # Verify password still works
            from app.security import verify_password
            if verify_password(DEV_CREATOR_PASSWORD, existing.password_hash):
                print("  Password: verified")
            else:
                print("  WARNING: Password hash doesn't match expected password!")
            return existing

        # Create the development creator account
        password_hash = hash_password(DEV_CREATOR_PASSWORD)

        user = User(
            username=DEV_CREATOR_USERNAME,
            email=DEV_CREATOR_EMAIL,
            password_hash=password_hash,
            role=DEV_CREATOR_ROLE,
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        print(f"✓ Created development creator account:")
        print(f"  Username: {DEV_CREATOR_USERNAME}")
        print(f"  Email: {DEV_CREATOR_EMAIL}")
        print(f"  Password: {DEV_CREATOR_PASSWORD}")
        print(f"  Role: {DEV_CREATOR_ROLE}")
        print(f"  ID: {user.id}")
        print()
        print("  Login with:")
        print(f"    curl -X POST http://127.0.0.1:8000/api/auth/login \\")
        print(f"      -H 'Content-Type: application/json' \\")
        print(f"      -d '{{\"username\":\"{DEV_CREATOR_USERNAME}\",\"password\":\"{DEV_CREATOR_PASSWORD}\"}}'")

        return user

    except Exception as e:
        db.rollback()
        print(f"✗ Error seeding development user: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print("Seeding development creator account...")
    print("=" * 50)
    seed_dev_user()
    print("=" * 50)
    print("Done.")