"""ASGI entrypoint for Chainlit on Vercel (single password-protected operator)."""

import asyncio
import os
from pathlib import Path
import shutil
import uuid

from dotenv import load_dotenv
import sys

load_dotenv()

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Chainlit creates .files and translations under APP_ROOT during import.
# Its read-only deployment files remain in the bundle; runtime files go to /tmp.
if os.environ.get("VERCEL"):
    for name in ("DATABASE_URL", "CHAINLIT_AUTH_SECRET", "DEXTER_STAGING_PASSWORD"):
        if not os.environ.get(name):
            raise RuntimeError(f"Missing required deployment environment variable: {name}")
    runtime = Path("/tmp/dexter/chainlit")
    runtime.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    for name in (".chainlit", "public"):
        shutil.copytree(source / name, runtime / name, dirs_exist_ok=True)
    shutil.copy2(source / "chainlit.md", runtime / "chainlit.md")
    os.environ["CHAINLIT_APP_ROOT"] = str(runtime)
    os.environ.setdefault("HF_HOME", "/tmp/dexter/huggingface")

from dexter import cloud_store
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from chainlit.server import UserParam, validate_file_upload
from chainlit.config import config
from chainlit.emitter import ChainlitEmitter
from chainlit.session import BaseSession
from chainlit.utils import mount_chainlit

app = FastAPI()


@app.get("/healthz")
def health():
    if cloud_store.ENABLED:
        with cloud_store.pool().connection() as conn:
            conn.execute("SELECT 1")
    return {"status": "ok"}


def store_transfer(file_id, session_id, owner, name, mime, content):
    cloud_store.put_object("transfer/" + file_id, content, {
        "session_id": session_id, "owner": owner, "name": name, "type": mime,
    })


if cloud_store.ENABLED:
    # HTTP uploads and the WebSocket can land on different Vercel instances.
    @app.post("/project/file")
    async def upload_file(current_user: UserParam, session_id: str, file: UploadFile,
                          ask_parent_id: str | None = None):
        if not current_user:
            raise HTTPException(401, "Authentication required")
        if ask_parent_id:
            raise HTTPException(400, "Use the attachment button to upload a file")
        try:
            uuid.UUID(session_id)
            validate_file_upload(file)
            content = await file.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise HTTPException(413, "Files must be 4 MB or smaller on this deployment")
            file_id = str(uuid.uuid4())
            await asyncio.to_thread(store_transfer, file_id, session_id,
                current_user.identifier, file.filename or "attachment",
                file.content_type or "application/octet-stream", content)
            return {"id": file_id}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()

    @app.get("/project/file/{file_id}")
    async def download_file(file_id: str, session_id: str, current_user: UserParam):
        stored = await asyncio.to_thread(cloud_store.get_object, "transfer/" + file_id)
        if not current_user or not stored or stored["metadata"]["owner"] != current_user.identifier:
            raise HTTPException(404, "File not found")
        if stored["metadata"]["session_id"] != session_id:
            raise HTTPException(404, "File not found")
        return Response(bytes(stored["data"]), media_type=stored["metadata"]["type"],
                        headers={"Cache-Control": "private, no-store"})

    original_persist = BaseSession.persist_file

    async def persist_file(self, *args, **kwargs):
        result = await original_persist(self, *args, **kwargs)
        file = self.files[result["id"]]
        await asyncio.to_thread(store_transfer, result["id"], self.id,
            self.user.identifier, file["name"], file["type"], Path(file["path"]).read_bytes())
        return result

    BaseSession.persist_file = persist_file
    original_process = ChainlitEmitter.process_message

    async def process_message(self, payload):
        for ref in payload.get("fileReferences") or []:
            file_id = str(uuid.UUID(ref["id"]))
            stored = await asyncio.to_thread(cloud_store.get_object, "transfer/" + file_id)
            if not stored or not self.session.user:
                raise ValueError("Attachment is unavailable")
            meta = stored["metadata"]
            if meta["owner"] != self.session.user.identifier or meta["session_id"] != self.session.id:
                raise ValueError("Attachment is unavailable")
            path = cloud_store.DATA_ROOT / "transfers" / file_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(stored["data"]))
            self.session.files[file_id] = {
                "id": file_id, "name": meta["name"], "type": meta["type"],
                "size": len(stored["data"]), "path": path,
            }
        return await original_process(self, payload)

    ChainlitEmitter.process_message = process_message

mount_chainlit(app, str(Path(__file__).with_name("chainlit_app.py")), path="")
config.project.transports = ["websocket"]
if os.environ.get("VERCEL"):
    config.features.spontaneous_file_upload.max_size_mb = 4
