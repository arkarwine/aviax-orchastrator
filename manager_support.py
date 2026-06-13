import json
import logging
import re
import shutil
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from bson import json_util
from pymongo import AsyncMongoClient


logger = logging.getLogger(__name__)


class DeploymentOperations:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._active: dict[str, str] = {}
        self._owners: dict[str, int] = {}
        self._depth: dict[str, int] = {}

    def current(self, name: str) -> Optional[str]:
        with self._guard:
            return self._active.get(name)

    @contextmanager
    def acquire(self, name: str, operation: str, *, token=None) -> Iterator[bool]:
        acquired = False
        owner_token = token if token is not None else ("thread", threading.get_ident())
        with self._guard:
            if name not in self._active:
                self._active[name] = operation
                self._owners[name] = owner_token
                self._depth[name] = 1
                acquired = True
            elif self._owners.get(name) == owner_token:
                self._depth[name] += 1
                acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                with self._guard:
                    self._depth[name] -= 1
                    if self._depth[name] <= 0:
                        self._active.pop(name, None)
                        self._owners.pop(name, None)
                        self._depth.pop(name, None)


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        *,
        issuer_id: Optional[int] = None,
        deployment: Optional[str] = None,
        result: str = "started",
        detail: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "issuer_id": issuer_id,
            "deployment": deployment,
            "result": result,
            "detail": detail[:500],
        }
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as audit_file:
                    audit_file.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            logger.exception("Could not write manager audit event %s.", action)


async def export_mongo_database(
    mongo_url: str,
    database_name: str,
    destination: Path,
) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=12500)
    manifest = {
        "database": database_name,
        "collections": {},
    }
    try:
        await client.admin.command("ping")
        database = client[database_name]
        for collection_name in sorted(await database.list_collection_names()):
            documents = []
            async for document in database[collection_name].find({}):
                documents.append(document)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", collection_name).strip("._") or "collection"
            output = destination / f"{safe_name}.json"
            output.write_text(
                json_util.dumps(documents, indent=2),
                encoding="utf-8",
            )
            manifest["collections"][collection_name] = {
                "file": output.name,
                "documents": len(documents),
            }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        return manifest
    finally:
        await client.close()


class RecoveryBackup:
    def __init__(
        self,
        *,
        root: Path,
        store,
        manager_env: Path,
        store_path: Path,
        sudo_store_path: Path,
        audit_path: Path,
        backup_state_path: Path,
        load_env,
    ) -> None:
        self.root = root
        self.store = store
        self.manager_env = manager_env
        self.store_path = store_path
        self.sudo_store_path = sudo_store_path
        self.audit_path = audit_path
        self.backup_state_path = backup_state_path
        self.load_env = load_env
        self.last_database_errors: dict[str, str] = {}

    def sources(self) -> list[tuple[Path, str]]:
        sources = []
        for path, archive_name in (
            (self.manager_env, "manager.env"),
            (self.store_path, "manager_deployments.json"),
            (self.sudo_store_path, "manager_sudoers.json"),
            (self.audit_path, "manager_audit.jsonl"),
            (self.backup_state_path, "manager_backup_state.json"),
        ):
            if path.is_file():
                sources.append((path, archive_name))
        for deployment in self.store.list().values():
            env_path = deployment.deployment_path / ".env"
            if env_path.is_file():
                sources.append((env_path, f"deployments/{deployment.name}/.env"))
            for session_path in deployment.deployment_path.glob("*.session*"):
                if session_path.is_file():
                    sources.append((session_path, f"deployments/{deployment.name}/{session_path.name}"))
        for session_path in self.root.glob("deploy-manager.session*"):
            if session_path.is_file():
                sources.append((session_path, session_path.name))
        for source_name in (
            "manager.py",
            "manager_support.py",
            "config.py",
            "deployment_launcher.py",
            "ecosystem.config.cjs",
            "requirements.txt",
            "start",
            "setup",
        ):
            source = self.root / source_name
            if source.is_file():
                sources.append((source, f"source/{source_name}"))
        for source_root in (self.root / "anony", self.root / "docs"):
            for source in source_root.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    sources.append((source, f"source/{source.relative_to(self.root).as_posix()}"))
        return sources

    async def create_archive(self) -> Path:
        self.last_database_errors = {}
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_path = Path(tempfile.gettempdir()) / f"aviax-manager-backup-{timestamp}.zip"
        deployments = self.store.list()
        staging = Path(tempfile.mkdtemp(prefix="aviax-db-backup-"))
        database_manifests = {}
        database_errors = {}
        exported = set()
        try:
            for deployment in deployments.values():
                env = self.load_env(deployment.deployment_path / ".env")
                mongo_url = env.get("MONGO_URL")
                db_name = deployment.db_name or env.get("DB_NAME")
                identity = (mongo_url, db_name)
                if not mongo_url or not db_name or identity in exported:
                    continue
                exported.add(identity)
                try:
                    database_manifests[deployment.name] = await export_mongo_database(
                        mongo_url,
                        db_name,
                        staging / "databases" / deployment.name,
                    )
                except Exception as exc:
                    logger.exception("Could not export database for %s", deployment.name)
                    database_errors[deployment.name] = f"{type(exc).__name__}: {exc}"

            (staging / "RECOVERY.md").write_text(
                "# Aviax Manager Disaster Recovery\n\n"
                "1. Install the bundled source and dependencies on the replacement server.\n"
                "2. Restore manager.env, manager_deployments.json, manager_sudoers.json, and deployment folders.\n"
                "3. Restore MongoDB with: python restore_mongo.py --mongo-url '<replacement MongoDB URL>'.\n"
                "   Warning: the restore helper replaces collections with the backed-up contents.\n"
                "4. Verify deployment paths and SESSION_PATH values for the replacement server.\n"
                "5. Start the manager with ecosystem.config.cjs, then inspect /list and /status.\n\n"
                "This archive contains credentials, Telegram sessions, and database data. Keep it private.\n",
                encoding="utf-8",
            )
            (staging / "restore_mongo.py").write_text(
                "import argparse, json\n"
                "from pathlib import Path\n"
                "from bson import json_util\n"
                "from pymongo import MongoClient\n\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--mongo-url', required=True)\n"
                "parser.add_argument('--backup-dir', default='databases')\n"
                "args = parser.parse_args()\n"
                "client = MongoClient(args.mongo_url)\n"
                "for deployment in Path(args.backup_dir).iterdir():\n"
                "    manifest_path = deployment / 'manifest.json'\n"
                "    if not manifest_path.exists():\n"
                "        continue\n"
                "    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))\n"
                "    database = client[manifest['database']]\n"
                "    for collection, info in manifest['collections'].items():\n"
                "        documents = json_util.loads((deployment / info['file']).read_text(encoding='utf-8'))\n"
                "        database[collection].delete_many({})\n"
                "        if documents:\n"
                "            database[collection].insert_many(documents)\n"
                "        print(f\"Restored {manifest['database']}.{collection}: {len(documents)} documents\")\n",
                encoding="utf-8",
            )
            manifest = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "deployment_count": len(deployments),
                "deployments": sorted(deployments),
                "contains_secrets": True,
                "contents": [
                    "manager state and environment",
                    "manager and deployed-bot source code",
                    "deployment environments",
                    "Telegram session files",
                    "MongoDB Extended JSON exports",
                    "structured audit history",
                    "recovery instructions",
                    "MongoDB restore helper",
                ],
                "database_exports": database_manifests,
                "database_export_errors": database_errors,
                "excluded": ["logs", "downloads", "cache"],
            }
            self.last_database_errors = dict(database_errors)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, indent=2))
                for source, archive_name in self.sources():
                    archive.write(source, archive_name)
                for source in staging.rglob("*"):
                    if source.is_file():
                        archive.write(source, source.relative_to(staging).as_posix())
            return archive_path
        finally:
            shutil.rmtree(staging, ignore_errors=True)
