"""Background runs for the dashboard: each run executes in its own thread with
its own database connection, and reports progress through a thread-safe sink."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from leadscout.config import load_config
from leadscout.db.store import Store
from leadscout.run_manager import RunManager


class ThreadProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stages: dict[str, dict] = {}
        self.notes: list[str] = []
        self.current: str | None = None

    def stage(self, name: str, total: int, done: int = 0) -> None:
        with self._lock:
            self.stages[name] = {"total": total, "done": done, "started": time.time(), "start_done": done}
            self.current = name

    def advance(self, name: str, n: int = 1) -> None:
        with self._lock:
            if name in self.stages:
                self.stages[name]["done"] += n

    def note(self, text: str) -> None:
        with self._lock:
            if text not in self.notes:
                self.notes.append(text)

    def snapshot(self) -> tuple[dict, list[str], str | None]:
        with self._lock:
            return {k: dict(v) for k, v in self.stages.items()}, list(self.notes), self.current


@dataclass
class Job:
    run_id: int
    action: str = "execute"          # execute | recheck | mailboxes
    progress: ThreadProgress = field(default_factory=ThreadProgress)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    stop_requested: bool = False
    manager: RunManager | None = None
    thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def skip_discovery(self) -> None:
        if self.manager is not None:
            self.manager.request_skip_discovery()

    def stop(self) -> None:
        self.stop_requested = True
        if self.manager is not None:
            self.manager.request_stop()


class JobRegistry:
    """One registry per process (the dashboard caches it as a shared resource)."""

    def __init__(self, root: Path | None = None):
        self.root = root
        self.jobs: dict[int, Job] = {}
        self._lock = threading.Lock()

    def start(self, run_id: int, action: str = "execute") -> Job:
        with self._lock:
            current = self.jobs.get(run_id)
            if current and current.active:
                return current
            job = Job(run_id=run_id, action=action)
            job.thread = threading.Thread(target=self._work, args=(job,), daemon=True,
                                          name=f"leadscout-run-{run_id}")
            self.jobs[run_id] = job
            job.thread.start()
            return job

    def _work(self, job: Job) -> None:
        cfg = load_config(self.root)
        store = Store(cfg.db_path)
        manager = RunManager(cfg, store, job.progress)
        job.manager = manager
        if job.stop_requested:
            manager.request_stop()
        try:
            if job.action == "recheck":
                manager.recheck(job.run_id)
            elif job.action == "mailboxes":
                manager.check_mailboxes(job.run_id)
            else:
                manager.execute(job.run_id)
        except Exception as exc:  # noqa: BLE001 - reported in the UI and the run log
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            manager.close()
            store.close()
            job.finished_at = time.time()

    def get(self, run_id: int) -> Job | None:
        return self.jobs.get(run_id)

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.active]
