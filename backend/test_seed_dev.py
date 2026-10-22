#!/usr/bin/env python3
"""Tests for development seed script."""

import pytest
from app.database import SessionLocal
from app.models import User
from app.security import verify_password
from app.seed_dev import seed_dev_user, DEV_CREATOR_USERNAME, DEV_CREATOR_PASSWORD, DEV_CREATOR_EMAIL, DEV_CREATOR_ROLE


def test_seed_creates_user():
    """Test that seed_dev_user creates a creator account."""
    user = seed_dev_user()
    
    assert user is not None
    assert user.username == DEV_CREATOR_USERNAME
    assert user.email == DEV_CREATOR_EMAIL
    assert user.role == DEV_CREATOR_ROLE
    assert verify_password(DEV_CREATOR_PASSWORD, user.password_hash)


def test_seed_is_idempotent():
    """Test that running seed twice doesn't create duplicate users."""
    user1 = seed_dev_user()
    user2 = seed_dev_user()
    
    assert user1.id == user2.id
    assert user1.username == user2.username


def test_seed_password_works():
    """Test that the seeded password can be used for login."""
    user = seed_dev_user()
    
    # Verify password works
    assert verify_password(DEV_CREATOR_PASSWORD, user.password_hash)
    
    # Wrong password should fail
    assert not verify_password("wrongpassword", user.password_hash)


def test_seed_user_in_database():
    """Test that the user is actually in the database."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == DEV_CREATOR_USERNAME).first()
        assert user is not None
        assert user.email == DEV_CREATOR_EMAIL
        assert user.role == DEV_CREATOR_ROLE
    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
