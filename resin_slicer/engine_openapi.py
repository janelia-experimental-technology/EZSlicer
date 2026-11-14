"""The OpenAPI description of the engine's HTTP API (served at GET /api/openapi.json).

Keep it in step with resin_slicer/engine.py: tests/contract checks that every route the engine
answers is described here.
"""
from __future__ import annotations

from typing import Any

from . import __version__
from .engine_info import API_VERSION, CAPABILITIES

_OBJ = {"type": "object", "additionalProperties": True}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json_body(schema: dict[str, Any], description: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"required": True, "content": {"application/json": {"schema": schema}}}
    if description:
        body["description"] = description
    return body


def _ok(schema: dict[str, Any], description: str = "OK", stream: bool = False) -> dict[str, Any]:
    content: dict[str, Any] = {"application/json": {"schema": schema}}
    if stream:
        content["application/x-ndjson"] = {
            "schema": _ref("StreamMessage"),
            "description": "With Accept: application/x-ndjson or ?stream=1: one message per line as the job runs, the result last. Blank lines are keep-alives.",
        }
    return {"description": description, "content": content}


_ERRORS = {
    "400": {"$ref": "#/components/responses/Error"},
    "401": {"$ref": "#/components/responses/Error"},
    "422": {"$ref": "#/components/responses/Error"},
    "502": {"$ref": "#/components/responses/Error"},
}
_STREAM_PARAM = [{"name": "stream", "in": "query", "required": False, "schema": {"type": "string", "enum": ["1", "0"]}, "description": "1 = stream every message as NDJSON"}]


def _op(summary: str, ok: dict[str, Any], *, body: dict[str, Any] | None = None, public: bool = False, stream: bool = False, description: str = "") -> dict[str, Any]:
    op: dict[str, Any] = {"summary": summary, "responses": {"200": ok, **({} if public else _ERRORS)}}
    if description:
        op["description"] = description
    if body is not None:
        op["requestBody"] = body
    if public:
        op["security"] = []
    if stream:
        op["parameters"] = _STREAM_PARAM
    return op


def openapi_document() -> dict[str, Any]:
    printer = {
        "type": "object",
        "required": ["ip"],
        "properties": {
            "id": {"type": "string", "description": "the caller's own id, echoed back in status-all"},
            "ip": {"type": "string"},
            "protocol": {"type": "string", "enum": ["chitu", "sdcp"], "description": "chitu = Elegoo Jupiter (ChiTu LAN, CTB only); sdcp = Elegoo SDCP v3 (Mars 5 Ultra, GOO or CTB)"},
            "mainboardId": {"type": "string", "description": "SDCP mainboard id (from discovery)"},
        },
        "additionalProperties": True,
    }
    status = {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["idle", "printing", "paused", "busy", "unknown"]},
            "progress": {"type": ["number", "null"]},
            "filename": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    }
    slice_request = {
        "type": "object",
        "description": "The same request EZSlicer's window sends: a mesh (inputPath or models[]), printer settings, support settings and transform.",
        "properties": {
            "inputPath": {"type": "string", "description": "an STL/OBJ/3MF/STEP file on this PC"},
            "models": {"type": "array", "items": _OBJ, "description": "several arranged models: {inputPath, transform}"},
            "profile": {"type": "string", "description": "a built-in profile name (GET /api/v1/profiles)"},
            "printer": {"type": "object", "additionalProperties": True, "description": "machine overrides: resolutionX, sizeX, layerHeight, exposure, ..."},
            "support": {"type": "object", "additionalProperties": True},
            "transform": {"type": "object", "additionalProperties": True},
            "manualOnly": {"type": "boolean"},
            "outputPath": {"type": "string", "description": "slice only: where to write the .ctb / .goo"},
            "format": {"type": "string", "enum": ["ctb", "goo"], "default": "goo", "description": "slice only. The Jupiter needs ctb."},
            "previewDir": {"type": "string", "description": "slice only: folder for the layer images (default: a new temp folder)"},
            "previewScale": {"type": "integer", "minimum": 1, "default": 1, "description": "slice only: layer images are 1/N of the screen size"},
        },
        "additionalProperties": True,
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "EZSlicer engine",
            "version": API_VERSION,
            "description": (
                f"EZSlicer {__version__}'s headless engine. Start it with JET_ENGINE_TOKEN set: "
                "`python -m resin_slicer.engine --engine` (or `EZSlicer.exe --engine`). It prints "
                '{"engine","version","api","port"} on one line and listens on 127.0.0.1:port. '
                "Every route except the four discovery routes needs Authorization: Bearer <token>."
            ),
            "x-capabilities": list(CAPABILITIES),
        },
        "servers": [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "0", "description": "from the startup line"}}}],
        "security": [{"engineToken": []}],
        "paths": {
            "/api/health": {"get": _op("Is the engine up?", _ok(_ref("Health")), public=True)},
            "/api/version": {"get": _op("Engine id, version, commit and API version", _ok(_ref("Version")), public=True)},
            "/api/capabilities": {"get": _op("Feature names this engine supports", _ok(_ref("Capabilities")), public=True)},
            "/api/openapi.json": {"get": _op("This document", _ok(_OBJ), public=True)},
            "/api/v1/info": {"get": _op("The bridge's `info` answer (engine, version, commit, python)", _ok(_ref("Version")))},
            "/api/v1/profiles": {"get": _op("Built-in machine profiles", _ok({"type": "object", "properties": {"type": {"const": "profiles"}, "profiles": {"type": "object", "additionalProperties": _OBJ}}}))},
            "/api/v1/preview": {"post": _op(
                "Plan supports for the viewport",
                _ok(_ref("PreviewResult"), stream=True),
                body=_json_body(slice_request),
                stream=True,
                description="Streams `support` messages as each support is planned. Runs in its own process; hanging up a stream stops it.",
            )},
            "/api/v1/slice": {"post": _op(
                "Slice to a .ctb / .goo file",
                _ok(_ref("SliceResult"), stream=True),
                body=_json_body({**slice_request, "required": ["outputPath"]}),
                stream=True,
                description="Streams `progress` messages (\"writing layer 120/986\"). Runs in its own process; hanging up a stream stops it.",
            )},
            "/api/v1/printers/discover": {"post": _op(
                "Find resin printers (UDP 3000)",
                _ok({"type": "object", "properties": {"type": {"const": "printers"}, "printers": {"type": "array", "items": _ref("PrinterInfo")}}}),
                body=_json_body({"type": "object", "properties": {"targets": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "number", "default": 2.5, "maximum": 10}, "broadcast": {"type": "boolean", "default": True}}}),
            )},
            "/api/v1/printers/status": {"post": _op(
                "One printer's state",
                _ok({"type": "object", "properties": {"type": {"const": "printer-status"}, "status": status}}),
                body=_json_body({"type": "object", "required": ["printer"], "properties": {"printer": printer}}),
            )},
            "/api/v1/printers/status-all": {"post": _op(
                "Every printer's state at once; an unreachable printer is ok:false, not an error",
                _ok({"type": "object", "properties": {"type": {"const": "printer-statuses"}, "statuses": {"type": "array", "items": {"allOf": [status, {"type": "object", "properties": {"id": {}, "ok": {"type": "boolean"}, "error": {"type": "string"}}}]}}}}),
                body=_json_body({"type": "object", "required": ["printers"], "properties": {"printers": {"type": "array", "items": printer}}}),
            )},
            "/api/v1/printers/control": {"post": _op(
                "Pause, resume or stop the print",
                _ok({"type": "object", "properties": {"type": {"const": "printer-status"}, "status": status}}),
                body=_json_body({"type": "object", "required": ["printer", "action"], "properties": {"printer": printer, "action": {"type": "string", "enum": ["pause", "resume", "stop"]}}}),
            )},
            "/api/v1/printers/send": {"post": _op(
                "Upload a sliced file, optionally start it",
                _ok({"type": "object", "properties": {"type": {"const": "printer-sent"}, "remoteName": {"type": "string"}, "started": {"type": "boolean"}}, "additionalProperties": True}, stream=True),
                body=_json_body({"type": "object", "required": ["printer", "path"], "properties": {"printer": printer, "path": {"type": "string"}, "start": {"type": "boolean", "default": False}}}),
                stream=True,
                description="Refuses a file in the wrong format for the printer, and refuses while it is printing. Streams `progress` messages with `fraction`.",
            )},
        },
        "components": {
            "securitySchemes": {"engineToken": {"type": "http", "scheme": "bearer", "description": "JET_ENGINE_TOKEN, given to the engine when it was started"}},
            "responses": {"Error": {"description": "Something went wrong", "content": {"application/json": {"schema": _ref("Error")}}}},
            "schemas": {
                "Error": {"type": "object", "required": ["error"], "properties": {"error": _ref("ErrorBody")}},
                "ErrorBody": {"type": "object", "required": ["code", "message"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "details": _OBJ}},
                "Health": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}, "uptimeS": {"type": "number"}, "jobs": {"type": "integer"}}},
                "Version": {
                    "type": "object",
                    "required": ["app", "version", "commit", "api"],
                    "properties": {"app": {"const": "ezslicer"}, "version": {"type": "string"}, "commit": {"type": "string"}, "api": {"type": "string"}, "date": {"type": "string"}, "python": {"type": "string"}},
                },
                "Capabilities": {"type": "object", "required": ["api", "features"], "properties": {"api": {"type": "string"}, "features": {"type": "array", "items": {"type": "string"}}}},
                "PrinterInfo": {"type": "object", "properties": {"ip": {"type": "string"}, "protocol": {"type": "string"}, "name": {"type": "string"}, "model": {"type": "string"}, "firmware": {"type": "string"}, "mainboardId": {"type": "string"}}, "additionalProperties": True},
                "PreviewResult": {"type": "object", "properties": {"type": {"const": "preview"}, "bed": _OBJ, "bounds": _OBJ, "layers": {"type": "integer"}, "supports": {"type": "array", "items": _OBJ}, "braces": {"type": "array", "items": _OBJ}, "raft": {}, "supportCount": {"type": "integer"}, "report": _OBJ}, "additionalProperties": True},
                "SliceResult": {"type": "object", "properties": {"type": {"const": "done"}, "outputPath": {"type": "string"}, "layers": {"type": "integer"}, "supports": {"type": "integer"}, "materialMl": {"type": "number"}, "previewDir": {"type": "string"}, "report": {}}, "additionalProperties": True},
                "StreamMessage": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": ["progress", "support", "preview", "done", "printer-sent", "error"]}, "message": {"type": "string"}, "fraction": {"type": "number"}, "support": _OBJ, "error": _ref("ErrorBody")},
                    "additionalProperties": True,
                },
            },
        },
    }
