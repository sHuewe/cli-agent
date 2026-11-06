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
        from cli_agent.interfaces.web_ui import render_index_html
        return browser_security_headers(HTMLResponse(render_index_html(ui.get("ui_config"), project_template=ui.get("ui_template"))))

    async def ui_css(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui import APP_CSS
        return browser_security_headers(PlainTextResponse(APP_CSS, media_type="text/css"))

    async def ui_theme_css(request: Request) -> Response:
        from importlib.resources import files
        theme = request.path_params["theme"]
        if theme not in {"bright", "dark"}:
            return browser_security_headers(PlainTextResponse("Not found", status_code=404))
        resource = files("cli_agent.interfaces").joinpath("static", "themes", theme + ".css")
        return browser_security_headers(PlainTextResponse(
            resource.read_text(encoding="utf-8"), media_type="text/css",
        ))

    async def ui_project_css(request: Request) -> Response:
        name = request.path_params["name"] + ".css"
        data = ui.get("ui_stylesheets", {}).get(name)
        if data is None:
            return browser_security_headers(PlainTextResponse("Not found", status_code=404))
        return browser_security_headers(PlainTextResponse(data, media_type="text/css"))

    async def ui_icon_image(request: Request) -> Response:
        # Serve only the exact image read from private project configuration
        # at startup. Unknown filenames are never resolved from disk.
        asset = ui.get("ui_icon_asset")
        if asset is None or request.path_params["filename"] != asset[0]:
            return browser_security_headers(PlainTextResponse("Not found", status_code=404))
        return browser_security_headers(Response(asset[1], media_type=asset[2]))

    async def ui_js(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui_api import API_APP_JS
        return browser_security_headers(PlainTextResponse(API_APP_JS, media_type="application/javascript"))

    async def ui_i18n(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui_api import API_I18N_JS
        return browser_security_headers(PlainTextResponse(API_I18N_JS, media_type="application/javascript"))

    async def ui_locales(_request: Request) -> Response:
        from cli_agent.interfaces.web_ui_locales import available_locales
        return browser_security_headers(JSONResponse(available_locales()))

    async def ui_locale(request: Request) -> Response:
        from cli_agent.interfaces.web_ui_locales import get_locale
        catalog = get_locale(request.path_params["locale"])
        if catalog is None:
            return browser_security_headers(JSONResponse({"error": "Unknown locale."}, status_code=404))
        return browser_security_headers(JSONResponse(catalog))

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
                        return error(400, "Invalid session ticket.")
                    session_id, lease = registry.redeem(ticket)
                else:
                    session_id = body.get("session_id", ui["session_id"])
                    if not isinstance(session_id, str):
                        return error(400, "Invalid session ID.")
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
            return error(404, "Session not found.")
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
            return error(409, "Session could not be opened.")
        return JSONResponse({"session_id": session.id, "ticket": ticket}, status_code=201)

    async def ui_saved_sessions(_request: Request) -> Response:
        if service.session_storage is None:
            return error(503, "Saved sessions are not configured.")
        try:
            return JSONResponse({"sessions": service.session_storage.list()})
        except (OSError, ValueError):
            return error(500, "Unable to list saved sessions.")

    async def ui_save_session(request: Request) -> Response:
        if service.session_storage is None:
            return error(503, "Saved sessions are not configured.")
        registry = ui["registry"]
        try:
            async with checked_json(request) as body:
                if (set(body) - {"session_id", "name", "snapshot_id"}
                        or not isinstance(body.get("session_id"), str)
                        or not isinstance(body.get("name"), str)):
                    return error(400, "Invalid save request.")
                sid = body["session_id"]
                if not registry.is_owner(sid, request.headers.get("x-cli-agent-lease")):
                    return error(403, "No valid tab lease.")
                snapshot_id = body.get("snapshot_id")
                if snapshot_id is not None and not isinstance(snapshot_id, str):
                    return error(400, "Invalid saved session ID.")
                result = service.session_storage.save(
                    service.sessions[sid], body["name"], snapshot_id=snapshot_id)
            return JSONResponse(result, status_code=201)
        except KeyError:
            return error(404, "Session not found.")
        except (ValueError, OSError) as exc:
            return error(400, str(exc))
        except RuntimeError as exc:
            return error(409, str(exc))

    async def ui_load_saved_session(request: Request) -> Response:
        if service.session_storage is None:
            return error(503, "Saved sessions are not configured.")
        registry = ui["registry"]
        try:
            session = await service.restore_saved_session(request.path_params["snapshot_id"])
        except FileNotFoundError:
            return error(404, "Saved session not found.")
        except OverflowError:
            return error(429, "Maximum active sessions reached.")
        except (ValueError, KeyError, OSError):
            return error(400, "Invalid saved session.")
        except Exception:
            return error(503, "Unable to restore saved session.")
        registry.register(session.id)
        try:
            ticket = registry.invitation(session.id)
        except Exception:
            registry.forget(session.id)
            await service.remove(session.id)
            return error(409, "Unable to open restored session.")
        return JSONResponse({"session_id": session.id, "ticket": ticket}, status_code=201)

    async def ui_delete_saved_session(request: Request) -> Response:
        if service.session_storage is None:
            return error(503, "Saved sessions are not configured.")
        try:
            service.session_storage.delete(request.path_params["snapshot_id"])
            return Response(status_code=204)
        except FileNotFoundError:
            return error(404, "Saved session not found.")
        except (ValueError, OSError):
            return error(400, "Invalid saved session.")

    async def ui_session_ticket(request: Request) -> Response:
        assert ui is not None
        from cli_agent.interfaces.web_ui_sessions import SessionOccupiedError
        try:
            ticket = ui["registry"].invitation(request.path_params["session_id"])
        except KeyError:
            return error(404, "Session not found.")
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
                    return error(409, "Session is busy.")
            elif not registry.can_close(session_id):
                return error(409, "Session is open in another tab or busy.")
        except KeyError:
            return error(404, "Session not found.")
        registry.forget(session_id)
        if not await service.remove(session_id):
            return error(404, "Session not found.")
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
                    return error(400, "Text is too large.")
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
                    return error(400, "Invalid Web UI command.")
                sid = body["session_id"]
                if not ui["registry"].is_owner(
                    sid, request.headers.get("x-cli-agent-lease")
                ):
                    return error(403, "Session does not belong to this tab.")
                prompt = _web_ui_command_prompt(
                    body.get("command"), body.get("argument")
                )
            session = service.sessions[sid]
            if session._pending_frontend:
                return error(409, "Pending frontend tool calls must be answered first.")
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
            return error(404, "Session not found.")
        except RuntimeError:
            return error(409, "Session is busy.")
        except ValueError as exc:
            return error(400, str(exc))


    return {name: handler for name, handler in (
        ("browser_security_headers", browser_security_headers),
        ("ui_index", ui_index),
        ("ui_css", ui_css),
        ("ui_theme_css", ui_theme_css),
        ("ui_project_css", ui_project_css),
        ("ui_icon_image", ui_icon_image),
        ("ui_js", ui_js),
        ("ui_i18n", ui_i18n),
        ("ui_locales", ui_locales),
        ("ui_locale", ui_locale),
        ("ui_bootstrap", ui_bootstrap),
        ("ui_sessions", ui_sessions),
        ("ui_session_new", ui_session_new),
        ("ui_saved_sessions", ui_saved_sessions),
        ("ui_save_session", ui_save_session),
        ("ui_load_saved_session", ui_load_saved_session),
        ("ui_delete_saved_session", ui_delete_saved_session),
        ("ui_session_ticket", ui_session_ticket),
        ("ui_session_delete", ui_session_delete),
        ("ui_release", ui_release),
        ("ui_heartbeat", ui_heartbeat),
        ("ui_render", ui_render),
        ("ui_command", ui_command),
    )}
