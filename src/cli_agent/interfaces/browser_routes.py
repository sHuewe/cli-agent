"""API-backed browser asset, lease, session and command handlers."""
from __future__ import annotations
from typing import Any
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from cli_agent.interfaces.agent_api_sessions import MAX_PROMPT_BYTES


def build_browser_handlers(service: Any, ui: dict[str, Any], error: Any, checked_json: Any) -> dict[str, Any]:
    def browser_security_headers(response: Response) -> Response:
        from cli_agent.interfaces.web_ui import _security_headers
        response = _security_headers(response)
        # The API-backed browser makes same-origin fetch requests only.
        response.headers["Content-Security-Policy"] = (
            response.headers["Content-Security-Policy"].replace(" ws://127.0.0.1:*", "")
        )
        return response

    async def ui_index(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui import INDEX_HTML
        return browser_security_headers(HTMLResponse(INDEX_HTML))

    async def ui_css(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui import APP_CSS
        return browser_security_headers(PlainTextResponse(APP_CSS, media_type="text/css"))

    async def ui_js(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui_api import API_APP_JS
        return browser_security_headers(PlainTextResponse(API_APP_JS, media_type="application/javascript"))

    async def ui_bootstrap(request: Request) -> Response:
        assert ui is not None
        from cli_agent.interfaces.web_ui_sessions import SessionOccupiedError
        registry = ui["registry"]
        try:
            async with checked_json(request) as body:
                if set(body) - {"session_id", "ticket"}:
                    return error(400, "Unbekannte Session-Parameter.")
                ticket = body.get("ticket")
                if ticket is not None:
                    if not isinstance(ticket, str):
                        return error(400, "Ungültiges Session-Ticket.")
                    session_id, lease = registry.redeem(ticket)
                else:
                    session_id = body.get("session_id", ui["session_id"])
                    if not isinstance(session_id, str):
                        return error(400, "Ungültige Session-ID.")
                    lease = registry.claim(session_id)
            session = service.sessions[session_id]
            return JSONResponse({
                "session_id": session_id, "lease": lease,
                **ui["info"], "initial_messages": session.initial_messages,
                "history": session.ui_messages,
                "agui_messages": session.agui_messages,
                "pending_interrupt": session._pending_interrupt,
                "active_run": (session._run.id if session._run is not None
                               and not session._run.done else None),
            })
        except SessionOccupiedError as exc:
            return error(409, str(exc))
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_sessions(_request: Request) -> Response:
        assert ui is not None
        return JSONResponse({"sessions": ui["registry"].list_sessions()})

    async def ui_session_new(_request: Request) -> Response:
        assert ui is not None
        from cli_agent.interfaces.web_ui_sessions import SessionOccupiedError
        registry = ui["registry"]
        try:
            session = await service.create()
        except OverflowError:
            return error(429, "Maximale Anzahl von Sessions erreicht.")
        except Exception:
            return error(503, "Session konnte nicht gestartet werden.")
        registry.register(session.id)
        try:
            ticket = registry.invitation(session.id)
        except (SessionOccupiedError, KeyError):
            registry.forget(session.id)
            await service.remove(session.id)
            return error(409, "Session konnte nicht geöffnet werden.")
        return JSONResponse({"session_id": session.id, "ticket": ticket}, status_code=201)

    async def ui_session_ticket(request: Request) -> Response:
        assert ui is not None
        from cli_agent.interfaces.web_ui_sessions import SessionOccupiedError
        try:
            ticket = ui["registry"].invitation(request.path_params["session_id"])
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except SessionOccupiedError as exc:
            return error(409, str(exc))
        return JSONResponse({"ticket": ticket})

    async def ui_session_delete(request: Request) -> Response:
        assert ui is not None
        registry = ui["registry"]
        session_id = request.path_params["session_id"]
        try:
            owner = registry.is_owner(
                session_id, request.headers.get("x-cli-agent-lease"), refresh=False
            )
            if owner:
                target = service.sessions[session_id]
                if target._busy and target._pending_interrupt is None:
                    return error(409, "Session ist beschäftigt.")
            elif not registry.can_close(session_id):
                return error(409, "Session ist in einem anderen Tab geöffnet oder beschäftigt.")
        except KeyError:
            return error(404, "Session nicht gefunden.")
        registry.forget(session_id)
        if not await service.remove(session_id):
            return error(404, "Session nicht gefunden.")
        return Response(status_code=204)

    async def ui_release(request: Request) -> Response:
        assert ui is not None
        try:
            async with checked_json(request) as body:
                if set(body) != {"session_id"} or not isinstance(body["session_id"], str):
                    return error(400, "Session-ID erforderlich.")
                if not ui["registry"].release(
                    body["session_id"], request.headers.get("x-cli-agent-lease")
                ):
                    return error(409, "Keine aktive Session-Bindung.")
                return Response(status_code=204)
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_heartbeat(request: Request) -> Response:
        assert ui is not None
        try:
            async with checked_json(request) as body:
                if set(body) != {"session_id"} or not isinstance(body["session_id"], str):
                    return error(400, "Session-ID erforderlich.")
                if not ui["registry"].is_owner(
                    body["session_id"], request.headers.get("x-cli-agent-lease")
                ):
                    return error(409, "Session-Bindung ist abgelaufen.")
                return Response(status_code=204)
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_render(request: Request) -> Response:
        from cli_agent.interfaces.web_ui import _render_markdown
        try:
            async with checked_json(request) as body:
                if set(body) != {"text"} or not isinstance(body["text"], str):
                    return error(400, "Text erwartet.")
                if len(body["text"].encode("utf-8")) > MAX_PROMPT_BYTES:
                    return error(400, "Text zu groß.")
                return JSONResponse({"html": _render_markdown(body["text"])})
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_command(request: Request) -> Response:
        """CLI-only commands are intentionally outside the AG-UI contract."""
        assert ui is not None
        from cli_agent.interfaces.web_ui import _web_ui_command_prompt
        try:
            async with checked_json(request) as body:
                if (set(body) - {"session_id", "command", "argument"}
                        or not isinstance(body.get("session_id"), str)):
                    return error(400, "Ungültiger Web-UI-Befehl.")
                sid = body["session_id"]
                if not ui["registry"].is_owner(
                    sid, request.headers.get("x-cli-agent-lease")
                ):
                    return error(403, "Session gehört nicht diesem Tab.")
                prompt = _web_ui_command_prompt(
                    body.get("command"), body.get("argument")
                )
            session = service.sessions[sid]
            if session._pending_frontend:
                return error(409, "Offene Frontend-Tool-Aufrufe müssen zuerst beantwortet werden.")
            # Local commands still run on the same MCP-owning worker.
            run = session.submit(prompt, require_listener=False)
            while not run.done:
                run.changed.clear()
                if not run.done:
                    await run.changed.wait()
            if run.status != "completed":
                return error(503, run.error or "Befehl fehlgeschlagen.")
            return JSONResponse({"answer": run.answer or ""})
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except RuntimeError:
            return error(409, "Session ist beschäftigt.")
        except ValueError as exc:
            return error(400, str(exc))


    return {name: handler for name, handler in (
        ("browser_security_headers", browser_security_headers),
        ("ui_index", ui_index),
        ("ui_css", ui_css),
        ("ui_js", ui_js),
        ("ui_bootstrap", ui_bootstrap),
        ("ui_sessions", ui_sessions),
        ("ui_session_new", ui_session_new),
        ("ui_session_ticket", ui_session_ticket),
        ("ui_session_delete", ui_session_delete),
        ("ui_release", ui_release),
        ("ui_heartbeat", ui_heartbeat),
        ("ui_render", ui_render),
        ("ui_command", ui_command),
    )}
