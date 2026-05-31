import asyncio
import logging
import threading
import time

from open_terminal.sync import git_sync, setup_sync_logging

setup_sync_logging()
logger = logging.getLogger("open_terminal.sync")


class SyncDaemon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="SyncDaemon")
        self.stop_event = threading.Event()
        self.loop = asyncio.new_event_loop()

    def run(self):
        logger.info("GitHub sync daemon started; log=%s", git_sync.get_status().get("log"))
        asyncio.set_event_loop(self.loop)
        try:
            # Startup restore: pull remote data before the periodic loop so every
            # process restart has a chance to recover the latest data immediately.
            self.loop.run_until_complete(git_sync.init_repo())
        except Exception as e:
            logger.exception("Error initializing git sync repo: %s", e)

        while not self.stop_event.is_set():
            if git_sync.enabled:
                try:
                    logger.info("Running scheduled GitHub sync")
                    result = self.loop.run_until_complete(git_sync.sync())
                    logger.info("Scheduled GitHub sync result: %s", result)
                except Exception as e:
                    logger.exception("Error during git sync: %s", e)

            interval = max(1, int(git_sync.interval or 1))
            logger.info("Next GitHub sync in %s seconds", interval)
            waited = 0
            while waited < interval and not self.stop_event.is_set():
                time.sleep(1)
                waited += 1

        logger.info("GitHub sync daemon stopped")
        self.loop.close()

    def stop(self):
        self.stop_event.set()


_sync_daemon = None
_sync_lock = threading.Lock()


def start_daemon():
    global _sync_daemon
    if not git_sync.enabled or not git_sync.repo:
        logger.info("GitHub sync daemon not started: enabled=%s repo_configured=%s", git_sync.enabled, bool(git_sync.repo))
        return

    with _sync_lock:
        if _sync_daemon is None or not _sync_daemon.is_alive():
            logger.info("Starting GitHub sync daemon")
            _sync_daemon = SyncDaemon()
            _sync_daemon.start()


def stop_daemon():
    global _sync_daemon
    with _sync_lock:
        daemon = _sync_daemon
        if daemon and daemon.is_alive():
            logger.info("Stopping GitHub sync daemon")
            daemon.stop()
            daemon.join(timeout=5)
        _sync_daemon = None
