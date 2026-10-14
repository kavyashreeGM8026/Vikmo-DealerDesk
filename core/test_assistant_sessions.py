"""SessionStore: dealer binding, per-session locking, creation races, eviction.
Pure Python (no database) and deterministic: threads are coordinated with events."""
import threading

from django.test import SimpleTestCase

from core.services.assistant.sessions import SessionBusy, SessionDealerMismatch, SessionStore


def open_and_commit(store, session_id, dealer_id, history=None):
    with store.open(session_id, dealer_id) as session:
        session.commit(history if history is not None else [])


class SessionStoreTests(SimpleTestCase):
    def test_session_is_bound_to_its_first_dealer(self):
        store = SessionStore()
        open_and_commit(store, "s", 1)
        with self.assertRaises(SessionDealerMismatch):
            with store.open("s", 2):
                self.fail("must not enter")
        self.assertEqual(store.dealer_id("s"), 1)

    def test_history_survives_between_opens(self):
        store = SessionStore()
        open_and_commit(store, "s", 1, ["a", "b"])
        with store.open("s", 1) as session:
            self.assertEqual(session.history, ["a", "b"])

    def test_a_session_whose_turn_never_succeeded_leaves_no_trace(self):
        store = SessionStore()
        with store.open("s", 1):
            pass  # no commit: the turn failed
        self.assertEqual(len(store), 0)
        open_and_commit(store, "s", 2)  # so another dealer can use the id
        self.assertEqual(store.dealer_id("s"), 2)

    def test_global_lock_is_free_while_the_body_runs(self):
        store = SessionStore()
        with store.open("s", 1):
            self.assertFalse(store._lock.locked())

    def _hold(self, store, session_id, dealer_id=1):
        """Hold a session open in a background thread. Returns (release_event, thread)."""
        inside, release = threading.Event(), threading.Event()

        def worker():
            with store.open(session_id, dealer_id) as session:
                inside.set()
                release.wait(5)
                session.commit([])

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(inside.wait(5))
        return release, thread

    def test_other_sessions_are_not_blocked_by_a_slow_one(self):
        store = SessionStore(lock_timeout=0.2)
        release, thread = self._hold(store, "slow")
        try:
            with store.open("fast", 1):  # would raise SessionBusy if sessions were serialized
                pass
        finally:
            release.set()
            thread.join()

    def test_same_session_requests_are_serialized_and_time_out(self):
        store = SessionStore(lock_timeout=0.05)
        release, thread = self._hold(store, "s")
        try:
            with self.assertRaises(SessionBusy):
                with store.open("s", 1):
                    self.fail("must not enter")
        finally:
            release.set()
            thread.join()
        with store.open("s", 1):  # free again afterwards
            pass

    def test_a_waiting_request_runs_after_the_first_finishes(self):
        store = SessionStore(lock_timeout=10)
        order = []
        release, thread = self._hold(store, "s")

        def second():
            with store.open("s", 1) as session:
                order.append("second")
                session.commit([])

        waiter = threading.Thread(target=second)
        waiter.start()
        self.assertEqual(order, [])  # still waiting for the first request
        order.append("first-done")
        release.set()
        thread.join()
        waiter.join()
        self.assertEqual(order, ["first-done", "second"])

    def test_concurrent_turns_in_one_session_do_not_lose_history(self):
        store = SessionStore(lock_timeout=10)
        errors = []

        def turn(n):
            try:
                with store.open("s", 1) as session:
                    snapshot = list(session.history)
                    threading.Event().wait(0.01)  # widen the race window
                    session.commit(snapshot + [n])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=turn, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(store.history("s")), list(range(8)))

    def test_concurrent_creation_binds_the_session_to_exactly_one_dealer(self):
        store = SessionStore(lock_timeout=10)
        barrier = threading.Barrier(2)
        outcomes = {}

        def attempt(dealer_id):
            barrier.wait(5)
            try:
                open_and_commit(store, "new", dealer_id)
                outcomes[dealer_id] = "ok"
            except SessionDealerMismatch:
                outcomes[dealer_id] = "mismatch"

        threads = [threading.Thread(target=attempt, args=(d,)) for d in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes.values()), ["mismatch", "ok"])
        winner = next(d for d, result in outcomes.items() if result == "ok")
        self.assertEqual(store.dealer_id("new"), winner)

    def test_concurrent_creation_for_the_same_dealer_makes_one_session(self):
        store = SessionStore(lock_timeout=10)
        barrier = threading.Barrier(6)
        seen = []

        def attempt():
            barrier.wait(5)
            with store.open("new", 1) as session:
                seen.append(session)
                session.commit([])

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(store), 1)
        self.assertEqual(len({id(s) for s in seen}), 1)

    def test_oldest_idle_sessions_are_evicted(self):
        store = SessionStore(max_sessions=2)
        for name in ("s1", "s2", "s3"):
            open_and_commit(store, name, 1)
        self.assertEqual(len(store), 2)
        self.assertIsNone(store.dealer_id("s1"))
        self.assertEqual(store.dealer_id("s3"), 1)

    def test_a_session_in_use_is_never_evicted(self):
        store = SessionStore(max_sessions=2)
        with store.open("busy", 1) as session:
            session.commit(["keep me"])
            for name in ("s2", "s3", "s4"):
                open_and_commit(store, name, 1)
            self.assertEqual(store.dealer_id("busy"), 1)
        self.assertEqual(store.history("busy"), ["keep me"])
        self.assertLessEqual(len(store), 2)

    def test_a_session_waiting_for_its_lock_is_not_evicted(self):
        store = SessionStore(max_sessions=1, lock_timeout=10)
        release, thread = self._hold(store, "held")
        try:
            open_and_commit(store, "other", 1)  # over the limit: idle ones go, "held" stays
            self.assertEqual(store.dealer_id("held"), 1)
        finally:
            release.set()
            thread.join()
