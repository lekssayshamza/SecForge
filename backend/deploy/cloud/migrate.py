"""Verify/run existing additive migrations, never application initialization."""
from app.database import engine
from app.challenge_migration import migrate as challenge
from app.profile_migration import migrate as profile
from app.lab_time_migration import migrate as timing
from app.auth_migration import migrate as auth
from app.profile_v2_migration import migrate as profile_v2
from app.leaderboard_migration import migrate as leaderboard
from app.room_tasks_migration import migrate as tasks
from app.education_migration import migrate as education
from app.discovery_migration import migrate as discovery
from app.operations_migration import migrate as operations
from app.readiness_migration import migrate as readiness
from app.profile_privacy_migration import migrate as profile_privacy
from app.workspace_migration import migrate as workspace
from app.social_migration import migrate as social
from app.room_media_migration import migrate as room_media
from app.network_ports_migration import migrate as network_ports
from app.editable_answers_migration import migrate as editable_answers
from app.learner_answers_migration import migrate as learner_answers
with engine.begin() as connection:
    changed = learner_answers(connection)
print("learner-submissions", "applied" if changed else "already current")
# Install the nullable authoring column before the strict legacy ORM verifier.
with engine.begin() as connection:
    changed = editable_answers(connection)
print("editable-answers", "applied" if changed else "already current")
# The legacy challenge verifier compares against the current ORM columns. Add
# nullable release provenance before that verifier sees an older database.
for name, migration in [("profile-privacy", profile_privacy), ("education", education), ("operations", operations), ("readiness", readiness), ("discovery", discovery), ("tasks", tasks), ("creator-workspace", workspace), ("challenges", challenge), ("profiles", profile), ("lab-time", timing), ("authentication", auth), ("profiles-v2", profile_v2), ("leaderboard", leaderboard), ("social", social)]:
    with engine.begin() as connection:
        changed = migration(connection)
    print(name, "applied" if changed else "already current")
with engine.begin() as connection:
    changed = room_media(connection)
print("room-media", "applied" if changed else "already current")

from app.room_categories_migration import migrate as room_categories
with engine.begin() as connection:
    changed = room_categories(connection)
print("room-categories", "applied" if changed else "already current")

from app.creator_keys_migration import migrate as creator_keys
with engine.begin() as connection:
    changed = creator_keys(connection)
print("creator-keys", "applied" if changed else "already current")

with engine.begin() as connection:
    changed = network_ports(connection)
print("room-ports", "applied" if changed else "already current")
