"""SQLite: users/roles, drafts, photos, prompts, publications, audit_log, settings."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Статусы
DRAFT = "Draft"
PENDING = "PendingApproval"
CHANGES = "ChangesRequested"
APPROVED = "Approved"
SCHEDULED = "Scheduled"
PUBLISHING = "Publishing"
PUBLISHED = "Published"
REJECTED = "Rejected"
CANCELLED = "Cancelled"
FAILED = "Failed"

EDITABLE = {DRAFT, CHANGES, PENDING, APPROVED, SCHEDULED}
ACTIVE = (DRAFT, CHANGES, PENDING, APPROVED, SCHEDULED, FAILED, PUBLISHING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, role TEXT NOT NULL, name TEXT, active INTEGER NOT NULL DEFAULT 1,
  added_by INTEGER, added_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, creator_id INTEGER NOT NULL, creator_name TEXT,
  editor_id INTEGER, manager_chat_id INTEGER, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  fields TEXT NOT NULL DEFAULT '{}', card_message_id INTEGER, replace_pos INTEGER,
  submitted_at TEXT, submitted_version INTEGER,
  approved_by INTEGER, approved_by_name TEXT, approved_at TEXT, approved_version INTEGER,
  reject_reason TEXT, scheduled_at TEXT, target_chat_id TEXT, published_at TEXT, error TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL REFERENCES drafts(id),
  file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL, sort_key INTEGER NOT NULL,
  author_id INTEGER, media_group_id TEXT, src_message_id INTEGER, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_photos_draft ON photos(draft_id);
CREATE TABLE IF NOT EXISTS prompts(
  chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, draft_id INTEGER NOT NULL,
  kind TEXT NOT NULL, arg TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, version INTEGER, decision TEXT,
  user_id INTEGER, comment TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS publications(
  draft_id INTEGER PRIMARY KEY, version INTEGER, target_chat_id TEXT, album_ids TEXT, text_id INTEGER,
  status TEXT, error TEXT, published_at TEXT, media_group_id TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, draft_id INTEGER, action TEXT, details TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # В одном чате отвечаем только на вопросы последнего черновика (чистка старых записей).
        self.q("""UPDATE prompts SET active=0 WHERE active=1 AND draft_id <> (
                    SELECT p.draft_id FROM prompts p WHERE p.chat_id=prompts.chat_id AND p.active=1
                    ORDER BY p.created_at DESC, p.message_id DESC LIMIT 1)""")

    # --- low level
    def q(self, sql, params=()):
        return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        r = self.q(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.q(sql, params).fetchall()]

    # --- settings
    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    # --- users
    def set_user(self, user_id, role, name=None, added_by=None):
        self.q("""INSERT INTO users(user_id,role,name,active,added_by,added_at) VALUES(?,?,?,1,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, active=1,
                  name=COALESCE(excluded.name,users.name)""", (user_id, role, name, added_by, now_iso()))

    def remove_user(self, user_id):
        self.q("UPDATE users SET active=0 WHERE user_id=?", (user_id,))

    def user_exists(self, user_id):
        """Есть ли запись о пользователе, включая отозванных (active=0)."""
        return self.one("SELECT 1 AS x FROM users WHERE user_id=?", (user_id,)) is not None

    def get_role(self, user_id):
        r = self.one("SELECT role FROM users WHERE user_id=? AND active=1", (user_id,))
        return r["role"] if r else None

    def list_users(self):
        return self.all("SELECT * FROM users WHERE active=1 ORDER BY role, user_id")

    # --- drafts
    @staticmethod
    def _draft(row):
        if not row:
            return None
        row["fields"] = json.loads(row["fields"] or "{}")
        row["code"] = f"MV-{row['id']:04d}"
        return row

    def create_draft(self, type_, user_id, name, chat_id):
        ts = now_iso()
        cur = self.q("""INSERT INTO drafts(type,creator_id,creator_name,editor_id,manager_chat_id,status,version,
                        fields,created_at,updated_at) VALUES(?,?,?,?,?,?,1,'{}',?,?)""",
                     (type_, user_id, name, user_id, chat_id, DRAFT, ts, ts))
        return self.get_draft(cur.lastrowid)

    def get_draft(self, draft_id):
        return self._draft(self.one("SELECT * FROM drafts WHERE id=?", (draft_id,)))

    def update_draft(self, draft_id, **kw):
        if "fields" in kw:
            kw["fields"] = json.dumps(kw["fields"], ensure_ascii=False)
        kw["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.q(f"UPDATE drafts SET {cols} WHERE id=?", (*kw.values(), draft_id))

    def claim(self, draft_id, from_statuses, to_status, **kw):
        """Атомарная смена статуса (защита от двойного клика). True, если заявка захвачена."""
        kw["status"] = to_status
        kw["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in kw)
        marks = ",".join("?" * len(from_statuses))
        cur = self.q(f"UPDATE drafts SET {cols} WHERE id=? AND status IN ({marks})",
                     (*kw.values(), draft_id, *from_statuses))
        return cur.rowcount == 1

    def list_drafts(self, where="1=1", params=(), limit=30, offset=0):
        rows = self.all(f"SELECT * FROM drafts WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        (*params, limit, offset))
        return [self._draft(r) for r in rows]

    # --- photos
    def photos(self, draft_id):
        return self.all("SELECT * FROM photos WHERE draft_id=? ORDER BY sort_key, id", (draft_id,))

    def add_photo(self, draft_id, file_id, unique_id, author_id, media_group_id, message_id):
        self.q("""INSERT INTO photos(draft_id,file_id,file_unique_id,sort_key,author_id,media_group_id,
                  src_message_id,created_at) VALUES(?,?,?,?,?,?,?,?)""",
               (draft_id, file_id, unique_id, message_id, author_id, media_group_id, message_id, now_iso()))

    def replace_photo(self, photo_id, file_id, unique_id, author_id, media_group_id, message_id):
        self.q("""UPDATE photos SET file_id=?, file_unique_id=?, author_id=?, media_group_id=?, src_message_id=?
                  WHERE id=?""", (file_id, unique_id, author_id, media_group_id, message_id, photo_id))

    def delete_photo(self, photo_id):
        self.q("DELETE FROM photos WHERE id=?", (photo_id,))

    def clear_photos(self, draft_id):
        self.q("DELETE FROM photos WHERE draft_id=?", (draft_id,))

    # --- prompts
    def add_prompt(self, chat_id, message_id, draft_id, kind, arg=None, replace_kinds=None):
        # Новый вопрос закрывает вопросы других черновиков этого чата: иначе ответ уйдёт в старый черновик.
        self.q("UPDATE prompts SET active=0 WHERE chat_id=? AND draft_id<>?", (chat_id, draft_id))
        if replace_kinds:
            marks = ",".join("?" * len(replace_kinds))
            self.q(f"UPDATE prompts SET active=0 WHERE draft_id=? AND kind IN ({marks})", (draft_id, *replace_kinds))
        self.q("INSERT OR REPLACE INTO prompts VALUES(?,?,?,?,?,1,?)",
               (chat_id, message_id, draft_id, kind, arg, now_iso()))

    def get_prompt(self, chat_id, message_id):
        return self.one("SELECT * FROM prompts WHERE chat_id=? AND message_id=? AND active=1", (chat_id, message_id))

    def latest_prompt(self, chat_id, kinds):
        marks = ",".join("?" * len(kinds))
        return self.one(f"SELECT * FROM prompts WHERE chat_id=? AND active=1 AND kind IN ({marks}) "
                        "ORDER BY created_at DESC, message_id DESC", (chat_id, *kinds))

    def deactivate_prompt(self, chat_id, message_id):
        self.q("UPDATE prompts SET active=0 WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    def deactivate_draft_prompts(self, draft_id):
        self.q("UPDATE prompts SET active=0 WHERE draft_id=?", (draft_id,))

    def active_prompt(self, draft_id, kind):
        return self.one("SELECT * FROM prompts WHERE draft_id=? AND kind=? AND active=1 "
                        "ORDER BY message_id DESC", (draft_id, kind))

    # --- publications / audit
    def get_publication(self, draft_id):
        r = self.one("SELECT * FROM publications WHERE draft_id=?", (draft_id,))
        if r:
            r["album_ids"] = json.loads(r["album_ids"] or "[]")
        return r

    def save_publication(self, draft_id, **kw):
        if self.one("SELECT 1 AS x FROM publications WHERE draft_id=?", (draft_id,)) is None:
            self.q("INSERT INTO publications(draft_id,album_ids) VALUES(?,'[]')", (draft_id,))
        if "album_ids" in kw:
            kw["album_ids"] = json.dumps(kw["album_ids"])
        cols = ", ".join(f"{k}=?" for k in kw)
        self.q(f"UPDATE publications SET {cols} WHERE draft_id=?", (*kw.values(), draft_id))

    def audit(self, user_id, draft_id, action, details=""):
        self.q("INSERT INTO audit_log(ts,user_id,draft_id,action,details) VALUES(?,?,?,?,?)",
               (now_iso(), user_id, draft_id, action, str(details)[:1000]))
