from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def encode_lsp_message(msg: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message with LSP Content-Length header."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def decode_lsp_message(data: bytes) -> dict[str, Any]:
    """Decode an LSP message from raw bytes (header + body)."""
    header_end = data.index(b"\r\n\r\n")
    body = data[header_end + 4 :]
    return json.loads(body)



class ClangdClient:
    """Lightweight LSP client that communicates with clangd over stdio."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._started = False
        self._crash_count = 0

    async def start(self) -> None:
        """Start clangd subprocess and initialize the LSP session."""
        self._process = await asyncio.create_subprocess_exec(
            "clangd",
            "--background-index",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._request(
            "initialize",
            {
                "processId": None,
                "capabilities": {},
                "rootUri": f"file://{self._cwd}",
            },
        )
        await self._notify("initialized", {})
        self._started = True
        logger.info("clangd started and initialized")

    async def stop(self) -> None:
        """Shut down clangd."""
        if self._process and self._process.returncode is None:
            await self._request("shutdown", {})
            await self._notify("exit", {})
            self._process.terminate()
            await self._process.wait()
        if self._reader_task:
            self._reader_task.cancel()
        self._started = False

    async def _restart(self) -> bool:
        """Restart clangd after a crash. Returns False if max restarts exceeded."""
        self._crash_count += 1
        if self._crash_count > 1:
            return False
        logger.warning("clangd crashed, restarting (attempt %d)", self._crash_count)
        await self.start()
        return True

    async def _send(self, data: bytes) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        """Read LSP messages from clangd stdout."""
        assert self._process and self._process.stdout
        reader = self._process.stdout
        buf = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buf += chunk

                while b"\r\n\r\n" in buf:
                    header_end = buf.index(b"\r\n\r\n")
                    header = buf[:header_end].decode("ascii")

                    content_length = 0
                    for line in header.split("\r\n"):
                        if line.startswith("Content-Length:"):
                            content_length = int(line.split(":")[1].strip())
                            break

                    body_start = header_end + 4
                    if len(buf) < body_start + content_length:
                        break

                    body = buf[body_start : body_start + content_length]
                    buf = buf[body_start + content_length :]
                    msg = json.loads(body)

                    if "id" in msg and msg["id"] in self._pending:
                        self._pending[msg["id"]].set_result(msg)
        except asyncio.CancelledError:
            pass

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send an LSP request and wait for the response."""
        self._request_id += 1
        rid = self._request_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[rid] = future
        await self._send(encode_lsp_message(msg))
        try:
            response = await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            del self._pending[rid]
            raise
        finally:
            self._pending.pop(rid, None)
        if "error" in response:
            raise RuntimeError(f"LSP error: {response['error']}")
        return response.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send an LSP notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._send(encode_lsp_message(msg))

    def _text_document_position(
        self, file: str, line: int, character: int = 0
    ) -> dict[str, Any]:
        return {
            "textDocument": {"uri": f"file://{file}"},
            "position": {"line": line - 1, "character": character},
        }

    async def call_hierarchy(self, file: str, line: int, character: int = 0) -> str:
        """Get incoming call hierarchy for the symbol at the given position."""
        try:
            params = self._text_document_position(file, line, character)
            items = await self._request("textDocument/prepareCallHierarchy", params)
            if not items:
                return "No call hierarchy items found."
            incoming = await self._request(
                "callHierarchy/incomingCalls", {"item": items[0]}
            )
            return json.dumps(incoming, indent=2)
        except (asyncio.TimeoutError, RuntimeError) as e:
            return f"clangd error (falling back to LLM reasoning): {e}"

    async def go_to_definition(self, file: str, line: int, character: int = 0) -> str:
        """Get the definition location of the symbol at the given position."""
        try:
            params = self._text_document_position(file, line, character)
            result = await self._request("textDocument/definition", params)
            return json.dumps(result, indent=2)
        except (asyncio.TimeoutError, RuntimeError) as e:
            return f"clangd error (falling back to LLM reasoning): {e}"

    async def find_references(self, file: str, line: int, character: int = 0) -> str:
        """Find all references to the symbol at the given position."""
        try:
            params = self._text_document_position(file, line, character)
            params["context"] = {"includeDeclaration": True}
            result = await self._request("textDocument/references", params)
            return json.dumps(result, indent=2)
        except (asyncio.TimeoutError, RuntimeError) as e:
            return f"clangd error (falling back to LLM reasoning): {e}"

    async def document_symbols(self, file: str) -> str:
        """List all symbols in a file."""
        try:
            result = await self._request(
                "textDocument/documentSymbol",
                {
                    "textDocument": {"uri": f"file://{file}"},
                },
            )
            return json.dumps(result, indent=2)
        except (asyncio.TimeoutError, RuntimeError) as e:
            return f"clangd error (falling back to LLM reasoning): {e}"
