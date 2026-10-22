"""Single taxonomy shared with the frontend build; aliases are migration-only."""
import json,unicodedata
from pathlib import Path
CATEGORIES=tuple(json.loads(Path(__file__).with_name('category_taxonomy.json').read_text()))
SLUGS=tuple(c['slug'] for c in CATEGORIES)
def validate_category(value):
    if value not in SLUGS:raise ValueError('Choose a supported category slug')
    return value

def folded(value):return ' '.join(unicodedata.normalize('NFKC',value or '').strip().casefold().split())
ALIASES={folded(c[k]):c['slug'] for c in CATEGORIES for k in ('label','slug')}
ALIASES.update({'web':'web-security','web security':'web-security','network':'networking','crypto':'cryptography','forensics':'digital-forensics','soc':'security-operations-soc','ot/ics':'ot-ics-security','programming':'programming-scripting','scripting':'programming-scripting','pentesting':'penetration-testing'})
def legacy_category(value):return ALIASES.get(folded(value),'other')


def validate_categories(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 8 or any(not isinstance(v,str) for v in values) or len(set(values)) != len(values):
        raise ValueError('Choose one to eight distinct supported categories')
    for value in values:validate_category(value)
    return values


def set_room_categories(room, values):
    from app.models import RoomCategory
    validate_categories(values)
    room.category = values[0]  # Existing clients retain a stable primary category.
    existing = {link.category: link for link in room.category_links}
    room.category_links = [existing.get(value) or RoomCategory(category=value) for value in values]


def attach_categories(db, rows):
    """One bounded query for explicit public projections; never widens schemas."""
    from sqlalchemy import select
    from app.models import RoomCategory
    items = [dict(row) for row in rows]
    by_id = {row['id']: row for row in items}
    for row in items:row['categories'] = [row['category']]
    if by_id:
        for room_id, category in db.execute(select(RoomCategory.room_id, RoomCategory.category).where(RoomCategory.room_id.in_(by_id)).order_by(RoomCategory.category)):
            if category not in by_id[room_id]['categories']:by_id[room_id]['categories'].append(category)
    return items
