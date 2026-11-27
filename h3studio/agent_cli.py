"""H3 Creative Studio MCP stdio adapter. Run the command from Studio's AI clients dialog."""
import argparse
import http.client
import json
from pathlib import Path
import sys

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_LINE = 160 * 1024


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def exchange(path, method, params):
    try:
        pair = json.loads(path.read_text(encoding="utf-8"))
        port, token = pair["port"], pair["token"]
        if type(port) is not int or not 0 < port < 65536 or not isinstance(token, str):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("Pair this client in the open Studio project and copy its current configuration.") from None
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
    try:
        connection.request("POST", "/assistant", json.dumps({"method": method, "params": params}).encode(),
                           {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        try:
            result = json.loads(response.read())
        except (ValueError, UnicodeError):
            raise ValueError("The paired desktop session is unavailable. Reconnect from Studio.") from None
        if response.status != 200:
            if result.get("rpc_code") == -32602:
                raise ProtocolError(-32602, result["error"])
            raise ValueError(result.get("error", "The paired desktop rejected the request."))
        return result
    except (OSError, http.client.HTTPException):
        raise ValueError("The paired Studio window is offline. Open the project and reconnect.") from None
    finally:
        connection.close()


class Session:
    def __init__(self, path):
        self.path = path
        self.phase = "new"
        self.version = None

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            raise ProtocolError(-32600, "Expected a JSON-RPC request.")
        method = msg["method"]
        params = msg.get("params", {})
        if "id" not in msg:
            if method == "notifications/initialized" and self.phase == "initializing":
                self.phase = "ready"
            return None
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "Expected named parameters.")
        if type(msg["id"]) not in (str, int):
            raise ProtocolError(-32600, "Expected a string or integer request ID.")
        if method == "initialize":
            if self.phase != "new":
                raise ProtocolError(-32600, "Already initialized.")
            version = params.get("protocolVersion")
            if not isinstance(version, str) or not isinstance(params.get("clientInfo"), dict) or not isinstance(params.get("capabilities"), dict):
                raise ProtocolError(-32602, "Provide protocolVersion, capabilities and clientInfo.")
            self.phase = "initializing"
            self.version = version if version in VERSIONS else VERSIONS[0]
            result = {"protocolVersion": self.version,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "h3-creative-studio", "version": "1.0"},
                      "instructions": "Start with project_overview. Use the IDs and revisions returned by Studio. Proposal tools save drafts for the author to review."}
        elif method == "ping":
            result = {}
        elif self.phase != "ready":
            raise ProtocolError(-32002, "Initialize the client first.")
        elif method == "tools/list":
            try:
                result = exchange(self.path, method, {})
            except ValueError as exc:
                raise ProtocolError(-32001, str(exc)) from None
        elif method == "tools/call":
            if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
                raise ProtocolError(-32602, "Provide a tool name and named arguments.")
            try:
                data = exchange(self.path, method, {"name": params["name"], "arguments": params.get("arguments", {})})
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
                if self.version in ("2025-11-25", "2025-06-18"):
                    result["structuredContent"] = data
            except ValueError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            raise ProtocolError(-32601, "Method not supported.")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True, help="Session file from Studio's AI clients dialog")
    session = Session(parser.parse_args().session)
    stream = sys.stdin.buffer
    while line := stream.readline(MAX_LINE + 1):
        ident = None
        try:
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE + 1)
                raise ProtocolError(-32700, "Message too large.")
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise ProtocolError(-32700, "Invalid JSON.") from None
            if isinstance(msg, dict) and type(msg.get("id")) in (int, str):
                ident = msg["id"]
            response = session.handle(msg)
        except ProtocolError as exc:
            response = {"jsonrpc": "2.0", "id": ident, "error": {"code": exc.code, "message": str(exc)}}
        if response is not None:
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
