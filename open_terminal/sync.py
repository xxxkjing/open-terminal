import asyncio
import os
import time
import logging
from typing import Tuple

from open_terminal.env import (
    GITHUB_SYNC_ENABLED,
    GITHUB_SYNC_INTERVAL,
    GITHUB_SYNC_EXCLUDE_PATTERNS,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GITHUB_SYNC_CWD
)

logger = logging.getLogger(__name__)

class GitSync:
    def __init__(self):
        self.enabled = GITHUB_SYNC_ENABLED
        self.repo = GITHUB_REPO
        self.token = GITHUB_TOKEN
        self.interval = GITHUB_SYNC_INTERVAL
        self.exclude = GITHUB_SYNC_EXCLUDE_PATTERNS
        self.cwd = os.path.abspath(GITHUB_SYNC_CWD)
        
        self._sync_task = None
        self._last_sync_time = None
        self._last_sync_status = "Not started"
        
        # Parse owner/repo from GITHUB_REPO if it's not a full URL
        if self.repo and not self.repo.startswith("http"):
            self.remote_url = f"https://{self.token}@github.com/{self.repo}.git" if self.token else f"https://github.com/{self.repo}.git"
        else:
            # If it's a URL, we might need to inject the token
            self.remote_url = self.repo

    async def run_cmd(self, cmd: str) -> Tuple[int, str, str]:
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def init_repo(self):
        if not self.enabled or not self.repo:
            return

        # Ensure cwd exists
        os.makedirs(self.cwd, exist_ok=True)

        # Check if already a git repo
        code, _, _ = await self.run_cmd("git status")
        if code != 0:
            logger.info("Initializing git repository for sync...")
            await self.run_cmd("git init")
            await self.run_cmd('git config user.name "Auto Sync"')
            await self.run_cmd('git config user.email "auto-sync@open-terminal.local"')
            
            # Setup exclusions
            if self.exclude:
                excludes = [e.strip() for e in self.exclude.split(",")]
                gitignore_path = os.path.join(self.cwd, ".gitignore")
                existing = ""
                if os.path.exists(gitignore_path):
                    with open(gitignore_path, "r") as f:
                        existing = f.read()
                
                with open(gitignore_path, "a") as f:
                    for ext in excludes:
                        if ext not in existing:
                            f.write(f"\n{ext}")
            
            # Add remote
            if self.remote_url:
                await self.run_cmd(f"git remote add origin {self.remote_url}")
                # Try to pull to get history
                await self.run_cmd("git fetch origin")
                # Try to set main as default branch and pull
                await self.run_cmd("git branch -M main")
                await self.run_cmd("git pull --rebase origin main")
        else:
            # Update remote url if needed
            if self.remote_url:
                await self.run_cmd(f"git remote set-url origin {self.remote_url}")

    async def sync(self) -> dict:
        if not self.enabled or not self.repo:
            return {"status": "disabled"}

        try:
            # Add changes
            await self.run_cmd("git add .")
            
            # Check if there are changes to commit
            code, stdout, _ = await self.run_cmd("git status --porcelain")
            if not stdout.strip():
                self._last_sync_status = "Success (No changes)"
                self._last_sync_time = time.time()
                return {"status": "no_changes"}
            
            # Commit
            commit_msg = f"Auto-sync update {time.strftime('%Y-%m-%d %H:%M:%S')}"
            code, stdout, stderr = await self.run_cmd(f'git commit -m "{commit_msg}"')
            if code != 0:
                self._last_sync_status = f"Commit failed: {stderr}"
                return {"status": "error", "error": f"Commit failed: {stderr}"}
            
            # Pull with rebase
            await self.run_cmd("git pull --rebase origin main")
            
            # Push
            code, stdout, stderr = await self.run_cmd("git push origin main")
            if code != 0:
                # If push fails, we might be on a different branch name or missing upstream
                code, stdout, stderr = await self.run_cmd("git push -u origin HEAD:main")
                if code != 0:
                    self._last_sync_status = f"Push failed: {stderr}"
                    return {"status": "error", "error": f"Push failed: {stderr}"}
            
            self._last_sync_status = "Success"
            self._last_sync_time = time.time()
            return {"status": "success", "message": "Synced successfully"}
            
        except Exception as e:
            self._last_sync_status = f"Error: {str(e)}"
            logger.error(f"Sync error: {e}")
            return {"status": "error", "error": str(e)}

    async def _sync_loop(self):
        await self.init_repo()
        while self.enabled:
            await asyncio.sleep(self.interval)
            await self.sync()

    def start(self):
        if self.enabled and self.repo:
            if self._sync_task is None or self._sync_task.done():
                self._sync_task = asyncio.create_task(self._sync_loop())

    def stop(self):
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            self._sync_task = None

    def get_status(self):
        return {
            "enabled": self.enabled,
            "repo": self.repo,
            "interval": self.interval,
            "last_sync_time": self._last_sync_time,
            "last_sync_status": self._last_sync_status,
        }

git_sync = GitSync()
