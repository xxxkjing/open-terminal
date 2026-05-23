import threading
import time
import logging
import asyncio
from open_terminal.sync import git_sync
from open_terminal.env import GITHUB_SYNC_ENABLED

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
            logger.error(f"Error initializing git sync repo: {e}")
            
        while not self.stop_event.is_set():
            if git_sync.enabled:
                try:
                    self.loop.run_until_complete(git_sync.sync())
                except Exception as e:
                    logger.error(f"Error during git sync: {e}")
            
            # Wait for interval or stop_event
            # Sleep in small chunks to be responsive to stop_event
            interval = git_sync.interval
            waited = 0
            while waited < interval and not self.stop_event.is_set():
                time.sleep(1)
                waited += 1
                
        self.loop.close()

    def stop(self):
        self.stop_event.set()

sync_daemon = SyncDaemon()

def start_daemon():
    if GITHUB_SYNC_ENABLED and not sync_daemon.is_alive():
        sync_daemon.start()

def stop_daemon():
    if sync_daemon.is_alive():
        sync_daemon.stop()
        sync_daemon.join(timeout=5)
