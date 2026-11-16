"""Resolve the recorded version of an entity, including bounded new-character supplements."""
from .relations import TABLES, resolve


def entity_version(conn, entity_type=None, name=None):
    row = conn.execute("SELECT value FROM meta WHERE key='game_version'").fetchone()
    default = row[0] if row else None
    if not name or entity_type not in TABLES:
        return default
    entity = resolve(conn, entity_type, name)
    if 'error' in entity:
        return None
    table = TABLES[entity_type]
    # Older fixture/schema readers may have only an ID and name.
    if 'game_version' not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
        return default
    row = conn.execute(f'SELECT game_version FROM {table} WHERE id=?', (entity['id'],)).fetchone()
    return row[0] if row and row[0] else default
