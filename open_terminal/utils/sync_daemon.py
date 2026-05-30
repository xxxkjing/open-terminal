import asyncio
import logging
import threading
import time

from open_terminal.sync import git_sync

logger = logging.getLogger(__name__)


class SyncDaemon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="SyncDaemon")
        self.stop_event = threading.Event()
        self.loop = asyncio.new_event_loop()

    def run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(git_sync.init_repo())
        except Exception as e:
            logger.error("Error initializing git sync repo: %s", e)

        while not self.stop_event.is_set():
            if git_sync.enabled:
                try:
                    self.loop.run_until_complete(git_sync.sync())
                except Exception as e:
                    logger.error("Error during git sync: %s", e)

            interval = max(1, int(git_sync.interval or 1))
            waited = 0
            while waited < interval and not self.stop_event.is_set():
                time.sleep(1)
                waited += 1

        self.loop.close()

    def stop(self):
        self.stop_event.set()


_sync_daemon = None
_sync_lock = threading.Lock()


def start_daemon():
    global _sync_daemon
    if not git_sync.enabled or not git_sync.repo:
        return

    with _sync_lock:
        if _sync_daemon is None or not _sync_daemon.is_alive():
            _sync_daemon = SyncDaemon()
            _sync_daemon.start()


def stop_daemon():
    global _sync_daemon
    with _sync_lock:
        daemon = _sync_daemon
        if daemon and daemon.is_alive():
            daemon.stop()
            daemon.join(timeout=5)
        _sync_daemon = None
