import asyncio
import logging
import os
import re
import shlex
import time
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

from open_terminal.env import (
    GITHUB_REPO,
    GITHUB_SYNC_CWD,
    GITHUB_SYNC_ENABLED,
    GITHUB_SYNC_EXCLUDE_PATTERNS,
    GITHUB_SYNC_INTERVAL,
    GITHUB_TOKEN,
    LOG_DIR,
)

logger = logging.getLogger(__name__)


def setup_sync_logging() -> str:
    """Ensure GitHub sync messages are always written to a log file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, "sync.log")

    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == log_path:
            return log_path

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = True
    return log_path


SYNC_LOG_PATH = setup_sync_logging()


class GitSync:
    def __init__(self):
        self.enabled = GITHUB_SYNC_ENABLED
        self.repo = GITHUB_REPO
        self.token = GITHUB_TOKEN
        self.interval = GITHUB_SYNC_INTERVAL
        self.exclude = GITHUB_SYNC_EXCLUDE_PATTERNS
        self.cwd = os.path.abspath(GITHUB_SYNC_CWD)
        self.branch = os.environ.get("OPEN_TERMINAL_GITHUB_BRANCH", "main")

        self._sync_task = None
        self._last_sync_time = None
        self._last_sync_status = "Not started"
        self._last_sync_log = SYNC_LOG_PATH
        self._lock = asyncio.Lock()

        self.remote_url = self._build_remote_url(self.repo, self.token)

    def _sanitize(self, text: str) -> str:
        if not text:
            return text
        sanitized = text
        if self.token:
            sanitized = sanitized.replace(self.token, "***")
        sanitized = re.sub(r"https://[^\s/@]+@github\.com/", "https://***@github.com/", sanitized)
        return sanitized

    @staticmethod
    def _build_remote_url(repo: str, token: str) -> str:
        if not repo:
            return ""

        if repo.startswith("github.com/"):
            base_url = f"https://{repo}"
        elif not repo.startswith(("http://", "https://", "git@", "file://")) and not os.path.isabs(repo):
            repo = repo.removesuffix(".git")
            base_url = f"https://github.com/{repo}.git"
        else:
            base_url = repo

        if not token or base_url.startswith("git@"):
            return base_url

        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or parsed.hostname != "github.com":
            return base_url

        netloc = f"{token}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(scheme="https", netloc=netloc))

    async def run_cmd(self, *args: str, check: bool = False) -> Tuple[int, str, str]:
        display_cmd = " ".join(shlex.quote(arg) for arg in args)
        logger.info("Running command in %s: %s", self.cwd, self._sanitize(display_cmd))
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        out = self._sanitize(stdout.decode(errors="replace"))
        err = self._sanitize(stderr.decode(errors="replace"))
        if out.strip():
            logger.info("Command stdout: %s", out.strip())
        if err.strip():
            log = logger.error if process.returncode else logger.info
            log("Command stderr: %s", err.strip())
        logger.info("Command finished with code %s: %s", process.returncode, self._sanitize(display_cmd))
        if check and process.returncode != 0:
            raise RuntimeError(f"Command failed ({display_cmd}): {err or out}")
        return process.returncode, out, err

    async def _git(self, *args: str, check: bool = False) -> Tuple[int, str, str]:
        return await self.run_cmd("git", *args, check=check)

    async def _is_git_repo(self) -> bool:
        code, stdout, _ = await self._git("rev-parse", "--is-inside-work-tree")
        return code == 0 and stdout.strip() == "true"

    async def _remote_branch_exists(self, branch: str) -> bool:
        code, _, _ = await self._git("ls-remote", "--exit-code", "--heads", "origin", branch)
        return code == 0

    async def _detect_remote_default_branch(self) -> Optional[str]:
        code, stdout, _ = await self._git("remote", "show", "origin")
        if code != 0:
            return None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("HEAD branch:"):
                branch = line.split(":", 1)[1].strip()
                if branch and branch != "(unknown)":
                    return branch
        return None

    async def _ensure_gitignore(self) -> None:
        if not self.exclude:
            return
        excludes = [e.strip() for e in self.exclude.split(",") if e.strip()]
        if not excludes:
            return

        gitignore_path = os.path.join(self.cwd, ".gitignore")
        existing = ""
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r", encoding="utf-8") as f:
                existing = f.read()

        existing_lines = {line.strip() for line in existing.splitlines()}
        missing = [pattern for pattern in excludes if pattern not in existing_lines]
        if missing:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")

    async def init_repo(self):
        if not self.enabled or not self.repo:
            logger.info("GitHub sync initialization skipped: enabled=%s repo_configured=%s", self.enabled, bool(self.repo))
            return

        async with self._lock:
            logger.info("Initializing GitHub sync: cwd=%s repo=%s branch=%s log=%s", self.cwd, self.repo, self.branch, self._last_sync_log)
            os.makedirs(self.cwd, exist_ok=True)

            if not await self._is_git_repo():
                logger.info("Initializing git repository for sync...")
                await self._git("init", check=True)

            await self._git("config", "user.name", "Auto Sync")
            await self._git("config", "user.email", "auto-sync@open-terminal.local")
            await self._ensure_gitignore()

            if self.remote_url:
                code, _, _ = await self._git("remote", "get-url", "origin")
                if code == 0:
                    await self._git("remote", "set-url", "origin", self.remote_url, check=True)
                else:
                    await self._git("remote", "add", "origin", self.remote_url, check=True)

                await self._git("fetch", "origin", "--prune")
                remote_default = await self._detect_remote_default_branch()
                if remote_default:
                    self.branch = os.environ.get("OPEN_TERMINAL_GITHUB_BRANCH", remote_default)

                local_branch_code, _, _ = await self._git("rev-parse", "--verify", self.branch)
                if local_branch_code == 0:
                    await self._git("checkout", self.branch)
                else:
                    await self._git("checkout", "-B", self.branch)

                if await self._remote_branch_exists(self.branch):
                    logger.info("Restoring data from origin/%s on startup", self.branch)
                    await self._git("branch", "--set-upstream-to", f"origin/{self.branch}", self.branch)
                    await self._git("pull", "--rebase", "--autostash", "origin", self.branch, check=True)
                    self._last_sync_status = "Startup restore completed"
                    self._last_sync_time = time.time()
                else:
                    logger.info("Remote branch origin/%s does not exist yet; startup restore skipped", self.branch)

    async def sync(self) -> dict:
        if not self.enabled or not self.repo:
            logger.info("GitHub sync skipped: enabled=%s repo_configured=%s", self.enabled, bool(self.repo))
            return {"status": "disabled", "log": self._last_sync_log}

        async with self._lock:
            logger.info("Starting GitHub sync: cwd=%s repo=%s branch=%s", self.cwd, self.repo, self.branch)
            try:
                # init_repo also uses the same lock, so do the setup inline here.
                os.makedirs(self.cwd, exist_ok=True)
                if not await self._is_git_repo():
                    await self._git("init", check=True)
                await self._git("config", "user.name", "Auto Sync")
                await self._git("config", "user.email", "auto-sync@open-terminal.local")
                await self._ensure_gitignore()
                if self.remote_url:
                    code, _, _ = await self._git("remote", "get-url", "origin")
                    if code == 0:
                        await self._git("remote", "set-url", "origin", self.remote_url, check=True)
                    else:
                        await self._git("remote", "add", "origin", self.remote_url, check=True)

                local_branch_code, _, _ = await self._git("rev-parse", "--verify", self.branch)
                if local_branch_code == 0:
                    await self._git("checkout", self.branch)
                else:
                    await self._git("checkout", "-B", self.branch)
                await self._git("fetch", "origin", "--prune")
                remote_default = await self._detect_remote_default_branch()
                if remote_default and "OPEN_TERMINAL_GITHUB_BRANCH" not in os.environ:
                    self.branch = remote_default
                    local_branch_code, _, _ = await self._git("rev-parse", "--verify", self.branch)
                    if local_branch_code == 0:
                        await self._git("checkout", self.branch)
                    else:
                        await self._git("checkout", "-B", self.branch)

                if await self._remote_branch_exists(self.branch):
                    code, stdout, stderr = await self._git(
                        "pull", "--rebase", "--autostash", "origin", self.branch
                    )
                    if code != 0:
                        self._last_sync_status = f"Pull failed: {stderr or stdout}"
                        logger.error(self._last_sync_status)
                        return {"status": "error", "error": self._last_sync_status, "log": self._last_sync_log}
                else:
                    logger.info("Remote branch origin/%s does not exist yet; sync will create it on push", self.branch)

                await self._git("add", "-A", check=True)

                code, stdout, _ = await self._git("status", "--porcelain")
                if code != 0:
                    self._last_sync_status = "Status check failed"
                    logger.error(self._last_sync_status)
                    return {"status": "error", "error": self._last_sync_status, "log": self._last_sync_log}

                if stdout.strip():
                    commit_msg = f"Auto-sync update {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    code, stdout, stderr = await self._git("commit", "-m", commit_msg)
                    if code != 0:
                        self._last_sync_status = f"Commit failed: {stderr or stdout}"
                        logger.error(self._last_sync_status)
                        return {"status": "error", "error": self._last_sync_status, "log": self._last_sync_log}

                code, stdout, stderr = await self._git("push", "-u", "origin", f"HEAD:{self.branch}")
                if code != 0:
                    self._last_sync_status = f"Push failed: {stderr or stdout}"
                    logger.error(self._last_sync_status)
                    return {"status": "error", "error": self._last_sync_status, "log": self._last_sync_log}

                self._last_sync_status = "Success" if stdout.strip() else "Success (No changes)"
                self._last_sync_time = time.time()
                logger.info("GitHub sync completed successfully on branch %s: %s", self.branch, self._last_sync_status)
                return {"status": "success", "message": "Synced successfully", "branch": self.branch, "log": self._last_sync_log}

            except Exception as e:
                error = self._sanitize(str(e))
                self._last_sync_status = f"Error: {error}"
                logger.exception("Sync error: %s", error)
                return {"status": "error", "error": error, "log": self._last_sync_log}

    async def _sync_loop(self):
        logger.info("GitHub sync loop starting; interval=%s seconds", self.interval)
        await self.init_repo()
        await self.sync()
        while self.enabled:
            await asyncio.sleep(self.interval)
            await self.sync()
        logger.info("GitHub sync loop stopped")

    def start(self):
        if self.enabled and self.repo:
            if self._sync_task is None or self._sync_task.done():
                logger.info("Starting GitHub sync task")
                self._sync_task = asyncio.create_task(self._sync_loop())
        else:
            logger.info("GitHub sync task not started: enabled=%s repo_configured=%s", self.enabled, bool(self.repo))

    def stop(self):
        if self._sync_task and not self._sync_task.done():
            logger.info("Stopping GitHub sync task")
            self._sync_task.cancel()
            self._sync_task = None

    def get_status(self):
        return {
            "enabled": self.enabled,
            "repo": self.repo,
            "branch": self.branch,
            "interval": self.interval,
            "last_sync_time": self._last_sync_time,
            "last_sync_status": self._last_sync_status,
            "log": self._last_sync_log,
        }


git_sync = GitSync()
