"""In-memory assistant sessions with per-session locking.

* One short global lock protects only the dictionary (create, look up, evict). It is
  never held while a request talks to the LLM, so slow sessions do not block each other.
* Each session has its own lock. Two requests for the same session run one after the
  other, so conversation history cannot be corrupted. A request that waits too long
  gets `SessionBusy`.
* A session is bound to the dealer that created it. The binding is kept only once a
  turn has succeeded (`session.commit(...)`); a session whose first turn failed leaves
  no trace.
* Sessions in use are never evicted.

State lives in this process only: run a single worker process, or move the store to
the database or Redis.
"""
import threading
from collections import OrderedDict
from contextlib import contextmanager


class SessionDealerMismatch(Exception):
    """The session id already belongs to a different dealer."""


class SessionBusy(Exception):
    """Another request for the same session did not finish in time."""


class _Session:
    __slots__ = ("dealer_id", "history", "lock", "users", "committed")

    def __init__(self, dealer_id):
        self.dealer_id = dealer_id
        self.history = []
        self.lock = threading.Lock()
        self.users = 0  # requests that hold or are waiting for this session
        self.committed = False  # True after the first successful turn

    def commit(self, history):
        """Store the history of a successful turn. Call with the session lock held."""
        self.history = history
        self.committed = True


class SessionStore:
    def __init__(self, max_sessions=500, lock_timeout=30.0):
        self.max_sessions = max_sessions
        self.lock_timeout = lock_timeout
        self._sessions = OrderedDict()
        self._lock = threading.Lock()

    @contextmanager
    def open(self, session_id, dealer_id):
        """Yield the session with its lock held (history is `session.history`)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = _Session(dealer_id)
                self._sessions[session_id] = session
            elif session.dealer_id != dealer_id:
                raise SessionDealerMismatch(session_id)
            session.users += 1
            self._sessions.move_to_end(session_id)
            self._evict_locked()

        try:
            if not session.lock.acquire(timeout=self.lock_timeout):
                raise SessionBusy(session_id)
            try:
                yield session
            finally:
                session.lock.release()
        finally:
            with self._lock:
                session.users -= 1
                # A session that never completed a turn is not kept (unless a request
                # for it is still waiting, which then decides its fate).
                if (
                    not session.committed
                    and session.users == 0
                    and self._sessions.get(session_id) is session
                ):
                    del self._sessions[session_id]
                self._evict_locked()

    def _evict_locked(self):
        """Drop the oldest idle sessions while over the limit. Caller holds self._lock."""
        if len(self._sessions) <= self.max_sessions:
            return
        for key in list(self._sessions):
            if len(self._sessions) <= self.max_sessions:
                break
            if self._sessions[key].users == 0:
                del self._sessions[key]

    # -- read-only helpers (used by tests) ---------------------------------------------
    def dealer_id(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            return session.dealer_id if session else None

    def history(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            return list(session.history) if session else None

    def __len__(self):
        with self._lock:
            return len(self._sessions)

    def clear(self):
        with self._lock:
            self._sessions.clear()
