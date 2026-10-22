"""Explicit capabilities; administrative authority never implies room ownership."""
LEARNING_ROLES = ('learner', 'creator', 'admin')

def has_learning_access(user):
    return user is not None and user.role in LEARNING_ROLES

def has_creator_access(user):
    return user is not None and user.role in ('creator', 'admin')
