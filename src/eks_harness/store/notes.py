from __future__ import annotations

from eks_harness.api.errors import bad_request
from eks_harness.api.schemas import NoteOut, ts_to_datetime
from eks_harness.daemon import events as ev
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.repos import notes as notes_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.notes import Note
from eks_harness.db.repos.sessions import Session

MAX_NOTE_LENGTH = 20000


def note_out(note: Note) -> NoteOut:
    return NoteOut(id=note.id, session_id=note.session_id, lease_sid=note.lease_sid, author=note.author,
                   body=note.body, created_at=ts_to_datetime(note.created_at))


def add_note(db: Database, session: Session, body: str, author: str, lease_sid: str | None = None,
             events: EventBus | None = None, project_id: str | None = None) -> Note:
    text = (body or "").strip()
    if not text:
        raise bad_request("The note is empty.", error="empty_note")
    if len(text) > MAX_NOTE_LENGTH:
        raise bad_request(f"A note can be at most {MAX_NOTE_LENGTH} characters.", error="note_too_long")
    with db.transaction() as conn:
        note = notes_repo.insert(conn, session.id, author, text, lease_sid)
        sessions_repo.touch(conn, session.id, note.created_at, project_id=project_id)
    if events is not None:
        events.publish(ev.NOTE_CREATED, lease_sid=lease_sid, session_id=session.id, project_id=project_id,
                       actor=author, detail={"noteId": note.id, "body": text[:500]})
    return note


def session_notes(db: Database, session: Session) -> list[NoteOut]:
    return [note_out(n) for n in notes_repo.list_for_session(db.conn(), session.id)]
