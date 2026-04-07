import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Generator, Optional

import flet as ft
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_join(base: Path, rel_path: str) -> Path:
    rel_norm = rel_path.replace("\\", "/").lstrip("/")
    parts = [p for p in rel_norm.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="Invalid path")
    candidate = (base / Path(*parts)).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    return candidate


def human_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num} B"


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header.startswith("bytes=") or "-" not in range_header:
        raise HTTPException(status_code=416, detail="Invalid range")
    value = range_header[6:].strip()
    if "," in value:
        raise HTTPException(status_code=416, detail="Multiple ranges not supported")
    start_s, end_s = value.split("-", 1)
    if start_s == "":
        length = int(end_s)
        if length <= 0:
            raise HTTPException(status_code=416, detail="Invalid suffix range")
        start = max(file_size - length, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    if start < 0 or end < start or start >= file_size:
        raise HTTPException(status_code=416, detail="Range out of bounds")
    end = min(end, file_size - 1)
    return start, end


class UploadInitRequest(BaseModel):
    rel_path: str = Field(..., description="Path relative to storage root")
    total_size: int = Field(..., ge=0)
    chunk_size: int = Field(..., gt=0)
    total_chunks: int = Field(..., gt=0)
    sha256: Optional[str] = None


class UploadCompleteRequest(BaseModel):
    upload_id: str


class FileServer:
    def __init__(self, root_dir: Path, token: str = "", max_workers: int = 8):
        self.root_dir = root_dir.resolve()
        self.session_dir = self.root_dir / ".upload_sessions"
        self.token = token.strip()
        self.max_workers = max_workers
        self._msg_lock = threading.Lock()
        self._messages: list[dict] = []
        ensure_dir(self.root_dir)
        ensure_dir(self.session_dir)

        self.app = FastAPI(title="Flet File Server", version="1.0.0")
        self._register_routes()

    def _auth(self, request: Request) -> None:
        if not self.token:
            return
        header_token = request.headers.get("x-token", "")
        query_token = request.query_params.get("token", "")
        if header_token != self.token and query_token != self.token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _token_query(self) -> str:
        return f"&token={self.token}" if self.token else ""

    def _register_routes(self) -> None:
        app = self.app
        auth = Depends(self._auth)

        @app.get("/health")
        def health() -> dict:
            return {"ok": True}

        @app.get("/", response_class=HTMLResponse)
        def home(_: None = auth) -> str:
            token_hint = self.token
            return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>File Server</title>
  <style>
    body {{ font-family: "Segoe UI", sans-serif; margin: 16px; }}
    .row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }}
    button {{ padding: 6px 10px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: left; }}
    code {{ background: #f2f2f2; padding: 2px 4px; border-radius: 4px; }}
    #log {{ white-space: pre-wrap; background: #f7f7f7; padding: 8px; border-radius: 6px; min-height: 72px; }}
    .file-picker {{ display: inline-flex; align-items: center; gap: 6px; }}
    .file-picker input[type=file] {{ display: none; }}
    .file-picker label {{ padding: 6px 10px; border: 1px solid #aaa; border-radius: 4px; cursor: pointer; }}
  </style>
</head>
<body>
  <h2>HTTP File Server</h2>
  <div class="row">
    <label>Path: <code id="path"></code></label>
  </div>
  <div class="row">
    <button onclick="goUp()">Up</button>
    <button onclick="refreshList()">Refresh</button>
    <button onclick="openMessageDialog()">消息对话框</button>
    <input id="mkdirName" placeholder="new folder" />
    <button onclick="mkdir()">Create Folder</button>
  </div>
  <div class="row">
    <span class="file-picker">
      <label for="fileInput">选择文件</label>
      <input id="fileInput" type="file" multiple />
    </span>
    <span class="file-picker">
      <label for="folderInput">选择文件夹</label>
      <input id="folderInput" type="file" webkitdirectory directory multiple />
    </span>
    <button onclick="uploadFiles()">Upload Selected</button>
  </div>
  <div class="row">
    <label>Token:</label>
    <input id="token" value="{token_hint}" placeholder="optional token" />
  </div>
  <table>
    <thead>
      <tr><th>Name</th><th>Type</th><th>Size</th><th>Updated</th><th>Actions</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <h4>Logs</h4>
  <div id="log"></div>
  <dialog id="msgDialog">
    <h3>消息对话框</h3>
    <div class="row">
      <input id="msgSender" placeholder="发送者(可选)" />
      <button onclick="loadMessages()">刷新消息</button>
      <button onclick="closeMessageDialog()">关闭</button>
    </div>
    <div class="row">
      <input id="msgInput" placeholder="输入消息内容" style="min-width: 360px;" />
      <button onclick="sendMessage()">发送</button>
    </div>
    <div id="msgList" style="max-height: 240px; overflow: auto; border: 1px solid #ddd; padding: 8px;"></div>
  </dialog>
  <script>
    let currentPath = "";
    const chunkSize = 4 * 1024 * 1024;
    const parallel = 4;
    const logEl = document.getElementById("log");
    const pathEl = document.getElementById("path");
    const msgListEl = document.getElementById("msgList");

    function log(msg) {{
      logEl.textContent = `[${{new Date().toLocaleTimeString()}}] ${{msg}}\\n` + logEl.textContent;
    }}

    function tokenHeaders() {{
      const token = document.getElementById("token").value.trim();
      return token ? {{ "x-token": token }} : {{}};
    }}

    function tokenQuery() {{
      const token = document.getElementById("token").value.trim();
      return token ? `&token=${{encodeURIComponent(token)}}` : "";
    }}

    function joinPath(a, b) {{
      if (!a) return b || "";
      if (!b) return a;
      return (a + "/" + b).replace(/\\/+/g, "/").replace(/\/+/g, "/");
    }}

    async function refreshList() {{
      pathEl.textContent = "/" + currentPath;
      const resp = await fetch(`/list?path=${{encodeURIComponent(currentPath)}}${{tokenQuery()}}`, {{ headers: tokenHeaders() }});
      if (!resp.ok) {{
        log("List failed: " + (await resp.text()));
        return;
      }}
      const data = await resp.json();
      const rows = document.getElementById("rows");
      rows.innerHTML = "";
      data.entries.forEach((e) => {{
        const tr = document.createElement("tr");
        const name = document.createElement("td");
        name.textContent = e.name;
        const type = document.createElement("td");
        type.textContent = e.is_dir ? "dir" : "file";
        const size = document.createElement("td");
        size.textContent = e.human_size;
        const mtime = document.createElement("td");
        mtime.textContent = e.mtime;
        const actions = document.createElement("td");
        if (e.is_dir) {{
          const openBtn = document.createElement("button");
          openBtn.textContent = "Open";
          openBtn.onclick = () => {{
            currentPath = e.rel_path;
            refreshList();
          }};
          actions.appendChild(openBtn);
          const dlBtn = document.createElement("button");
          dlBtn.textContent = "Zip Download";
          dlBtn.onclick = () => {{
            const p = encodeURIComponent(e.rel_path);
            window.location.href = `/download-folder?path=${{p}}${{tokenQuery()}}`;
          }};
          actions.appendChild(dlBtn);
          const delDirBtn = document.createElement("button");
          delDirBtn.textContent = "Delete Folder";
          delDirBtn.onclick = async () => {{
            if (!confirm(`Delete folder recursively: ${{e.rel_path}} ?`)) return;
            const p = encodeURIComponent(e.rel_path);
            const r = await fetch(`/delete-folder?path=${{p}}${{tokenQuery()}}`, {{
              method: "DELETE",
              headers: tokenHeaders()
            }});
            if (!r.ok) {{
              log("Delete folder failed: " + (await r.text()));
              return;
            }}
            log("Deleted folder: " + e.rel_path);
            refreshList();
          }};
          actions.appendChild(delDirBtn);
        }} else {{
          const dlBtn = document.createElement("button");
          dlBtn.textContent = "Download";
          dlBtn.onclick = () => {{
            const p = encodeURIComponent(e.rel_path);
            window.location.href = `/download?path=${{p}}${{tokenQuery()}}`;
          }};
          actions.appendChild(dlBtn);
          const delBtn = document.createElement("button");
          delBtn.textContent = "Delete";
          delBtn.onclick = async () => {{
            if (!confirm(`Delete file: ${{e.rel_path}} ?`)) return;
            const p = encodeURIComponent(e.rel_path);
            const r = await fetch(`/delete?path=${{p}}${{tokenQuery()}}`, {{
              method: "DELETE",
              headers: tokenHeaders()
            }});
            if (!r.ok) {{
              log("Delete failed: " + (await r.text()));
              return;
            }}
            log("Deleted: " + e.rel_path);
            refreshList();
          }};
          actions.appendChild(delBtn);
        }}
        tr.appendChild(name);
        tr.appendChild(type);
        tr.appendChild(size);
        tr.appendChild(mtime);
        tr.appendChild(actions);
        rows.appendChild(tr);
      }});
    }}

    function goUp() {{
      if (!currentPath) return;
      const parts = currentPath.split("/").filter(Boolean);
      parts.pop();
      currentPath = parts.join("/");
      refreshList();
    }}

    async function mkdir() {{
      const name = document.getElementById("mkdirName").value.trim();
      if (!name) return;
      const relPath = joinPath(currentPath, name);
      const resp = await fetch(`/mkdir${{tokenQuery()}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", ...tokenHeaders() }},
        body: JSON.stringify({{ rel_path: relPath }})
      }});
      if (!resp.ok) {{
        log("Create folder failed: " + (await resp.text()));
        return;
      }}
      log("Created folder: " + relPath);
      document.getElementById("mkdirName").value = "";
      refreshList();
    }}

    async function uploadOneFile(file, relPath) {{
      const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
      const initResp = await fetch(`/upload/init${{tokenQuery()}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", ...tokenHeaders() }},
        body: JSON.stringify({{
          rel_path: relPath,
          total_size: file.size,
          chunk_size: chunkSize,
          total_chunks: totalChunks
        }})
      }});
      if (!initResp.ok) {{
        throw new Error("init failed: " + (await initResp.text()));
      }}
      const initData = await initResp.json();
      const uploaded = new Set(initData.uploaded_chunks || []);
      let next = 0;
      async function worker() {{
        while (true) {{
          const idx = next;
          next += 1;
          if (idx >= totalChunks) return;
          if (uploaded.has(idx)) continue;
          const start = idx * chunkSize;
          const end = Math.min(file.size, start + chunkSize);
          const chunk = file.slice(start, end);
          const fd = new FormData();
          fd.append("upload_id", initData.upload_id);
          fd.append("index", String(idx));
          fd.append("chunk", chunk, file.name + ".part" + idx);
          const r = await fetch(`/upload/chunk${{tokenQuery()}}`, {{
            method: "POST",
            headers: tokenHeaders(),
            body: fd
          }});
          if (!r.ok) {{
            throw new Error("chunk " + idx + " failed: " + await r.text());
          }}
        }}
      }}
      const workers = [];
      for (let i = 0; i < Math.min(parallel, totalChunks); i++) {{
        workers.push(worker());
      }}
      await Promise.all(workers);
      const doneResp = await fetch(`/upload/complete${{tokenQuery()}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", ...tokenHeaders() }},
        body: JSON.stringify({{ upload_id: initData.upload_id }})
      }});
      if (!doneResp.ok) {{
        throw new Error("complete failed: " + (await doneResp.text()));
      }}
    }}

    async function uploadFiles() {{
      const files = [];
      const normal = document.getElementById("fileInput").files;
      const folder = document.getElementById("folderInput").files;
      for (const f of normal) {{
        files.push({{ file: f, rel: joinPath(currentPath, f.name) }});
      }}
      for (const f of folder) {{
        const rel = f.webkitRelativePath || f.name;
        files.push({{ file: f, rel: joinPath(currentPath, rel) }});
      }}
      if (!files.length) {{
        log("No files selected");
        return;
      }}
      log(`Uploading ${{files.length}} files...`);
      const started = performance.now();
      let done = 0;
      for (const item of files) {{
        await uploadOneFile(item.file, item.rel);
        done += 1;
        log(`Uploaded ${{done}}/${{files.length}}: ${{item.rel}}`);
      }}
      const sec = (performance.now() - started) / 1000;
      log(`Finished. ${{files.length}} files in ${{sec.toFixed(1)}}s`);
      document.getElementById("fileInput").value = "";
      document.getElementById("folderInput").value = "";
      refreshList();
    }}

    function openMessageDialog() {{
      const dlg = document.getElementById("msgDialog");
      if (dlg && dlg.showModal) {{
        dlg.showModal();
      }}
      loadMessages();
    }}

    function closeMessageDialog() {{
      const dlg = document.getElementById("msgDialog");
      if (dlg) dlg.close();
    }}

    async function loadMessages() {{
      const resp = await fetch(`/messages${{tokenQuery()}}`, {{ headers: tokenHeaders() }});
      if (!resp.ok) {{
        log("Load messages failed: " + (await resp.text()));
        return;
      }}
      const data = await resp.json();
      const items = data.items || [];
      if (!items.length) {{
        msgListEl.textContent = "暂无消息";
        return;
      }}
      msgListEl.innerHTML = "";
      for (const m of items) {{
        const row = document.createElement("div");
        row.style.borderBottom = "1px solid #eee";
        row.style.padding = "6px 0";
        row.textContent = `[${{m.time}}] ${{m.sender}}: ${{m.text}}`;
        msgListEl.appendChild(row);
      }}
    }}

    async function sendMessage() {{
      const textEl = document.getElementById("msgInput");
      const senderEl = document.getElementById("msgSender");
      const text = (textEl.value || "").trim();
      if (!text) return;
      const sender = (senderEl.value || "").trim() || "anonymous";
      const resp = await fetch(`/messages${{tokenQuery()}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", ...tokenHeaders() }},
        body: JSON.stringify({{ sender, text }})
      }});
      if (!resp.ok) {{
        log("Send message failed: " + (await resp.text()));
        return;
      }}
      textEl.value = "";
      await loadMessages();
    }}

    refreshList();
  </script>
</body>
</html>
"""

        @app.get("/list")
        def list_dir(path: str = "", _: None = auth) -> JSONResponse:
            target = safe_join(self.root_dir, path)
            if not target.exists():
                raise HTTPException(status_code=404, detail="Path not found")
            if not target.is_dir():
                raise HTTPException(status_code=400, detail="Path is not a directory")

            entries = []
            for p in target.iterdir():
                if p.name == ".upload_sessions":
                    continue
                stat = p.stat()
                rel = p.relative_to(self.root_dir).as_posix()
                entries.append(
                    {
                        "name": p.name,
                        "rel_path": rel,
                        "is_dir": p.is_dir(),
                        "size": stat.st_size if p.is_file() else 0,
                        "human_size": human_size(stat.st_size) if p.is_file() else "-",
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    }
                )

            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return JSONResponse({"path": path, "entries": entries})

        @app.get("/messages")
        def get_messages(_: None = auth) -> dict:
            with self._msg_lock:
                items = list(self._messages[-100:])
            return {"items": items}

        @app.post("/messages")
        async def post_message(payload: dict, _: None = auth) -> dict:
            text = (payload.get("text") or "").strip()
            sender = (payload.get("sender") or "anonymous").strip()[:32] or "anonymous"
            if not text:
                raise HTTPException(status_code=400, detail="text is required")
            item = {
                "id": int(time.time() * 1000),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "sender": sender,
                "text": text[:1000],
            }
            with self._msg_lock:
                self._messages.append(item)
                if len(self._messages) > 200:
                    self._messages = self._messages[-200:]
            return {"ok": True, "item": item}

        @app.delete("/delete")
        def delete_file(path: str, _: None = auth) -> dict:
            rel = path.strip()
            if not rel:
                raise HTTPException(status_code=400, detail="path is required")
            target = safe_join(self.root_dir, rel)
            if not target.exists():
                raise HTTPException(status_code=404, detail="Path not found")
            if not target.is_file():
                raise HTTPException(status_code=400, detail="Only file deletion is supported")
            target.unlink()
            return {"ok": True, "path": target.relative_to(self.root_dir).as_posix()}

        @app.delete("/delete-folder")
        def delete_folder(path: str, _: None = auth) -> dict:
            rel = path.strip()
            if not rel:
                raise HTTPException(status_code=400, detail="path is required")
            target = safe_join(self.root_dir, rel)
            if not target.exists():
                raise HTTPException(status_code=404, detail="Path not found")
            if not target.is_dir():
                raise HTTPException(status_code=400, detail="Only folder deletion is supported")
            if target.resolve() == self.root_dir.resolve():
                raise HTTPException(status_code=400, detail="Root folder cannot be deleted")
            shutil.rmtree(target)
            return {"ok": True, "path": rel}

        @app.post("/mkdir")
        async def mkdir(payload: dict, _: None = auth) -> dict:
            rel_path = (payload.get("rel_path") or "").strip()
            if not rel_path:
                raise HTTPException(status_code=400, detail="rel_path is required")
            target = safe_join(self.root_dir, rel_path)
            ensure_dir(target)
            return {"ok": True, "path": rel_path}

        @app.post("/upload")
        async def upload(
            file: UploadFile = File(...),
            rel_path: str = Form(""),
            _: None = auth,
        ) -> dict:
            if rel_path:
                target = safe_join(self.root_dir, rel_path)
            else:
                target = safe_join(self.root_dir, file.filename or "unknown.bin")
            ensure_dir(target.parent)
            with target.open("wb") as fw:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    fw.write(chunk)
            return {"ok": True, "path": target.relative_to(self.root_dir).as_posix()}

        @app.post("/upload/init")
        async def upload_init(payload: UploadInitRequest, _: None = auth) -> dict:
            target = safe_join(self.root_dir, payload.rel_path)
            ensure_dir(target.parent)

            upload_key = (
                f"{payload.rel_path}|{payload.total_size}|"
                f"{payload.total_chunks}|{payload.chunk_size}"
            )
            upload_id = hashlib.sha1(upload_key.encode("utf-8")).hexdigest()
            session_path = self.session_dir / upload_id
            chunks_path = session_path / "chunks"
            ensure_dir(chunks_path)

            meta_path = session_path / "meta.json"
            if meta_path.exists():
                with meta_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
                if (
                    meta.get("rel_path") != payload.rel_path
                    or int(meta.get("total_size", -1)) != payload.total_size
                    or int(meta.get("total_chunks", -1)) != payload.total_chunks
                    or int(meta.get("chunk_size", -1)) != payload.chunk_size
                ):
                    raise HTTPException(status_code=409, detail="Upload session metadata mismatch")
            else:
                meta = {
                    "upload_id": upload_id,
                    "rel_path": payload.rel_path,
                    "total_size": payload.total_size,
                    "chunk_size": payload.chunk_size,
                    "total_chunks": payload.total_chunks,
                    "sha256": payload.sha256,
                    "created_at": int(time.time()),
                }
                with meta_path.open("w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=True, indent=2)

            uploaded_chunks = []
            for c in chunks_path.glob("*.part"):
                try:
                    uploaded_chunks.append(int(c.stem))
                except ValueError:
                    pass
            uploaded_chunks.sort()

            return {"ok": True, "upload_id": upload_id, "uploaded_chunks": uploaded_chunks}

        @app.post("/upload/chunk")
        async def upload_chunk(
            upload_id: str = Form(...),
            index: int = Form(...),
            chunk: UploadFile = File(...),
            _: None = auth,
        ) -> dict:
            session_path = self.session_dir / upload_id
            meta_path = session_path / "meta.json"
            if not meta_path.exists():
                raise HTTPException(status_code=404, detail="Upload session not found")
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            total_chunks = int(meta["total_chunks"])
            if index < 0 or index >= total_chunks:
                raise HTTPException(status_code=400, detail="Chunk index out of range")

            chunk_path = session_path / "chunks" / f"{index}.part"
            with chunk_path.open("wb") as fw:
                while True:
                    data = await chunk.read(1024 * 1024)
                    if not data:
                        break
                    fw.write(data)
            return {"ok": True, "upload_id": upload_id, "index": index}

        @app.get("/upload/status/{upload_id}")
        def upload_status(upload_id: str, _: None = auth) -> dict:
            session_path = self.session_dir / upload_id
            meta_path = session_path / "meta.json"
            if not meta_path.exists():
                raise HTTPException(status_code=404, detail="Upload session not found")
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            uploaded_chunks = []
            for c in (session_path / "chunks").glob("*.part"):
                try:
                    uploaded_chunks.append(int(c.stem))
                except ValueError:
                    pass
            uploaded_chunks.sort()
            return {"ok": True, "meta": meta, "uploaded_chunks": uploaded_chunks}

        @app.post("/upload/complete")
        def upload_complete(payload: UploadCompleteRequest, _: None = auth) -> dict:
            session_path = self.session_dir / payload.upload_id
            meta_path = session_path / "meta.json"
            if not meta_path.exists():
                raise HTTPException(status_code=404, detail="Upload session not found")
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)

            target = safe_join(self.root_dir, meta["rel_path"])
            ensure_dir(target.parent)
            total_chunks = int(meta["total_chunks"])
            chunks_dir = session_path / "chunks"
            missing = [i for i in range(total_chunks) if not (chunks_dir / f"{i}.part").exists()]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing chunks: {missing[:5]}{'...' if len(missing) > 5 else ''}",
                )

            temp_target = target.with_suffix(target.suffix + ".uploading")
            with temp_target.open("wb") as fw:
                for i in range(total_chunks):
                    part = chunks_dir / f"{i}.part"
                    with part.open("rb") as fr:
                        shutil.copyfileobj(fr, fw, length=4 * 1024 * 1024)

            if meta.get("sha256"):
                h = hashlib.sha256()
                with temp_target.open("rb") as fr:
                    while True:
                        b = fr.read(4 * 1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                if h.hexdigest().lower() != meta["sha256"].lower():
                    temp_target.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail="SHA256 mismatch")

            os.replace(temp_target, target)

            for c in chunks_dir.glob("*.part"):
                c.unlink(missing_ok=True)
            try:
                (session_path / "meta.json").unlink(missing_ok=True)
                chunks_dir.rmdir()
                session_path.rmdir()
            except OSError:
                pass

            return {"ok": True, "path": target.relative_to(self.root_dir).as_posix()}

        @app.get("/download")
        def download(path: str, request: Request, _: None = auth):
            target = safe_join(self.root_dir, path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            file_size = target.stat().st_size
            range_header = request.headers.get("range")

            def file_iterator(start: int, end: int) -> Generator[bytes, None, None]:
                with target.open("rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            if range_header:
                start, end = parse_range_header(range_header, file_size)
                headers = {
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(end - start + 1),
                    "Content-Disposition": f'attachment; filename="{target.name}"',
                }
                return StreamingResponse(
                    file_iterator(start, end),
                    status_code=206,
                    media_type="application/octet-stream",
                    headers=headers,
                )

            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Disposition": f'attachment; filename="{target.name}"',
            }
            return StreamingResponse(
                file_iterator(0, file_size - 1),
                media_type="application/octet-stream",
                headers=headers,
            )

        @app.get("/download-folder")
        def download_folder(path: str, _: None = auth):
            target = safe_join(self.root_dir, path)
            if not target.exists() or not target.is_dir():
                raise HTTPException(status_code=404, detail="Folder not found")
            tmp_fd, tmp_zip = tempfile.mkstemp(prefix="folder_", suffix=".zip")
            os.close(tmp_fd)
            zip_path = Path(tmp_zip)

            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for p in target.rglob("*"):
                    if p.is_file():
                        arcname = p.relative_to(target.parent).as_posix()
                        zf.write(p, arcname=arcname)

            filename = f"{target.name}.zip"
            return FileResponse(
                path=zip_path,
                filename=filename,
                media_type="application/zip",
                background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
            )


class ServerController:
    def __init__(self):
        self.server: Optional[uvicorn.Server] = None
        self.thread: Optional[threading.Thread] = None
        self.file_server: Optional[FileServer] = None
        self.host = "0.0.0.0"
        self.port = 8080
        self.root_dir = Path.cwd() / "data"

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, host: str, port: int, root_dir: Path, token: str, max_workers: int) -> None:
        if self.running():
            raise RuntimeError("Server is already running")

        self.host = host
        self.port = port
        self.root_dir = root_dir.resolve()
        ensure_dir(self.root_dir)
        self.file_server = FileServer(self.root_dir, token=token, max_workers=max_workers)
        config = uvicorn.Config(
            app=self.file_server.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            workers=1,
        )
        self.server = uvicorn.Server(config)

        def run():
            self.server.run()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

        deadline = time.time() + 5
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("Server start timeout")

    def stop(self) -> None:
        if not self.running():
            return
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.thread = None
        self.server = None
        self.file_server = None

    def urls(self) -> dict:
        lan_ips: set[str] = set()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    lan_ips.add(ip)
        except OSError:
            pass

        try:
            host_name = socket.gethostname()
            _, _, ips = socket.gethostbyname_ex(host_name)
            for ip in ips:
                if ip and not ip.startswith("127."):
                    lan_ips.add(ip)
        except OSError:
            pass

        lan_list = sorted(lan_ips) if lan_ips else ["127.0.0.1"]
        return {
            "local": f"http://127.0.0.1:{self.port}",
            "lan_list": [f"http://{ip}:{self.port}" for ip in lan_list],
            "public_hint": f"http://<public-ip-or-domain>:{self.port}",
        }


def main(page: ft.Page):
    page.title = "Flet HTTP File Server"
    page.window_width = 900
    page.window_height = 760
    page.scroll = ft.ScrollMode.AUTO

    controller = ServerController()
    log_view = ft.TextField(
        value="",
        multiline=True,
        min_lines=8,
        max_lines=12,
        read_only=True,
        expand=True,
    )

    host_input = ft.TextField(label="Host", value="0.0.0.0", width=180)
    port_input = ft.TextField(label="Port", value="8080", width=120)
    root_input = ft.TextField(label="Storage Root", value=str((Path.cwd() / "data").resolve()), expand=True)
    token_input = ft.TextField(label="Access Token (Optional)", password=False, can_reveal_password=True, expand=True)
    workers_input = ft.TextField(label="Parallel Workers", value="8", width=180)
    status_text = ft.Text("Status: stopped")
    url_text = ft.Text("URLs: -")

    def add_log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        log_view.value = f"[{ts}] {msg}\n" + log_view.value
        page.update()

    def on_start(_):
        try:
            host = host_input.value.strip() or "0.0.0.0"
            port = int((port_input.value or "8080").strip())
            root = Path(root_input.value.strip() or str(Path.cwd() / "data")).resolve()
            workers = int((workers_input.value or "8").strip())
            token = token_input.value.strip()

            controller.start(host, port, root, token, workers)
            urls = controller.urls()
            lan_lines = "\n".join(urls["lan_list"])
            status_text.value = f"Status: running on {host}:{port}"
            url_text.value = (
                f"Local: {urls['local']}\n"
                f"LAN (all):\n{lan_lines}\n"
                f"Public(需端口映射/反向代理): {urls['public_hint']}"
            )
            add_log(f"Server started. Root={root}")
            add_log("支持断点续传下载 (HTTP Range) 与分块并行上传。")
        except Exception as e:
            add_log(f"Start failed: {e}")

    def on_stop(_):
        controller.stop()
        status_text.value = "Status: stopped"
        url_text.value = "URLs: -"
        add_log("Server stopped.")
        page.update()

    def open_root(_):
        root = Path(root_input.value.strip() or str(Path.cwd() / "data")).resolve()
        ensure_dir(root)
        os.startfile(str(root))
        add_log(f"Opened: {root}")

    start_btn = ft.ElevatedButton("Start Server", on_click=on_start)
    stop_btn = ft.OutlinedButton("Stop Server", on_click=on_stop)
    open_btn = ft.TextButton("Open Storage Folder", on_click=open_root)

    page.add(
        ft.Column(
            controls=[
                ft.Text("Flet + HTTP 文件服务器", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("功能: 文件/文件夹上传下载、目录结构保持、断点续传、局域网/公网可访问"),
                ft.Row([host_input, port_input, workers_input], wrap=True),
                root_input,
                token_input,
                ft.Row([start_btn, stop_btn, open_btn], wrap=True),
                status_text,
                url_text,
                ft.Divider(),
                ft.Text("Web 客户端地址: 启动后访问 /"),
                ft.Text("API: /list /upload/init /upload/chunk /upload/complete /download /download-folder"),
                ft.Divider(),
                ft.Text("Logs"),
                log_view,
            ],
            spacing=10,
            tight=True,
        )
    )


if __name__ == "__main__":
    ft.app(target=main)
