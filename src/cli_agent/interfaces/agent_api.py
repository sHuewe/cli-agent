"""HTTP, AG-UI and browser routes for the local agent service."""
from cli_agent.interfaces.agent_api_sessions import *

def create_app(service: AgentApiService, *, ui: dict[str, Any] | None = None) -> Any:
    """Create a loopback-only ASGI app. Browser mode shares the same API service."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.routing import Route
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            await service.shutdown()

    @asynccontextmanager
    async def checked_json(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type application/json erforderlich.")
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise ValueError("Ungültiger Content-Length-Header.") from exc
            if length < 0:
                raise ValueError("Ungültiger Content-Length-Header.")
            if length > MAX_JSON_BYTES:
                raise ValueError("JSON-Anfrage zu groß.")

        # Content-Length is optional (for example with chunked encoding), and
        # client-provided length is not a trusted memory limit. Check the size
        # of each incoming chunk BEFORE appending it to the buffered JSON.
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > MAX_JSON_BYTES:
                raise ValueError("JSON-Anfrage zu groß.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Ungültiges JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Ein JSON-Objekt wird benötigt.")
        yield value

    @asynccontextmanager
    async def session_from(request: Request):
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            raise KeyError("Session nicht gefunden.")
        yield session

    async def create_session(_request: Request) -> Response:
        try:
            session = await service.create_standalone_session()
        except OverflowError:
            return error(429, "Zu viele Sessions.")
        except Exception:
            return error(503, "Agenten-Session konnte nicht gestartet werden.")
        return JSONResponse({"session_id": session.id}, status_code=201)

    async def delete_session(request: Request) -> Response:
        if not await service.remove(request.path_params["session_id"]):
            return error(404, "Session nicht gefunden.")
        return Response(status_code=204)

    async def delete_agui_thread(request: Request) -> Response:
        """Standalone AG-UI management extension (not part of AG-UI 1.0).

        A standalone session created via POST /sessions uses its own ID as
        threadId. Auto-created AG-UI threads retain their client's threadId
        and expose the internal ID via an authenticated response header.
        Browser sessions are not addressable through this endpoint.
        """
        thread_id = request.path_params["thread_id"]
        if not isinstance(thread_id, str) or not 0 < len(thread_id) <= 128:
            return error(400, "Ungültige AG-UI-threadId.")
        if not await service.remove_agui_thread(thread_id):
            return error(404, "AG-UI-Thread nicht gefunden.")
        return Response(status_code=204)

    async def download_artifact(request: Request) -> Response:
        """Authenticated localhost-only artifact access (outside AG-UI transport)."""
        thread_id = _artifact_thread_from_token(request.path_params["thread_id"])
        if thread_id is None:
            return error(404, "Download nicht gefunden.")
        artifact_id = request.path_params["artifact_id"]
        browser = ui is not None and request.headers.get("x-cli-agent-browser") == "1"
        if browser:
            assert ui is not None
            if not ui["registry"].is_owner(
                thread_id, request.headers.get("x-cli-agent-lease")
            ):
                return error(403, "Keine gültige Browser-Bindung.")
            session = service.sessions.get(thread_id)
        else:
            session_id = service._agui_threads.get(thread_id)
            session = service.sessions.get(session_id) if session_id else None
        if session is None or session.agent is None:
            return error(404, "Download nicht gefunden.")
        descriptor = session.published_artifacts.get(artifact_id)
        if descriptor is None:
            return error(404, "Download nicht gefunden.")
        try:
            payload = session.agent._result_artifacts.read_registered(descriptor["path"])
        except (ValueError, RuntimeError, OSError):
            return error(404, "Download nicht mehr verfügbar.")
        from urllib.parse import quote
        filename = descriptor["filename"]
        return Response(
            payload,
            media_type=descriptor["mime_type"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def ag_ui_capabilities(_request: Request) -> Response:
        """Optional application-specific discovery using AG-UI's standard shape.

        The AG-UI 1.0 specification defines AgentCapabilities but intentionally
        does not prescribe a discovery URL. Never advertise unsupported
        frontend tools, multimodal input or stream resumption.
        """
        return JSONResponse({
            "identity": {"name": "cli-agent", "type": "cli-agent", "version": "1.0.0"},
            "transport": {
                "streaming": True, "websocket": False,
                "httpBinary": False, "resumable": False,
            },
            "tools": {
                "supported": True, "clientProvided": True,
                "parallelCalls": False,
            },
            "output": {"supportedMimeTypes": ["text/plain"]},
            "state": {
                "snapshots": False, "deltas": False,
                "persistentState": True,
            },
            "humanInTheLoop": {
                "supported": True, "approvals": True,
                "interrupts": True, "approveWithEdits": False,
            },
        })

    async def ag_ui_run(request: Request) -> Response:
        """AG-UI 1.0 HTTP/SSE binding: one POST and one streamed response.

        Optional cli-agent configuration is deliberately kept on separate
        endpoints. AG-UI peers need no cli-agent-specific run or approval API.
        """
        from cli_agent.interfaces.ag_ui_protocol import parse_run_input, encode_sse
        try:
            async with checked_json(request) as body:
                # Application-specific control envelope on the existing AG-UI
                # URL. Normal RunAgentInput POSTs remain strictly AG-UI 1.0.
                if body.get("action") == "cancel":
                    if (
                        set(body) != {"action", "threadId", "runId"}
                        or not all(
                            isinstance(body.get(key), str)
                            and 0 < len(body[key]) <= 128
                            for key in ("threadId", "runId")
                        )
                    ):
                        return error(400, "Ungültige Cancel-Anfrage.")
                    thread_id, run_id = body["threadId"], body["runId"]
                    browser_cancel = (
                        ui is not None
                        and request.headers.get("x-cli-agent-browser") == "1"
                    )
                    if browser_cancel:
                        assert ui is not None
                        if not ui["registry"].is_owner(
                            thread_id, request.headers.get("x-cli-agent-lease")
                        ):
                            return error(403, "Keine gültige Browser-Bindung.")
                        target = service.sessions.get(thread_id)
                    else:
                        session_id = service._agui_threads.get(thread_id)
                        target = service.sessions.get(session_id) if session_id else None
                    if target is None:
                        return error(404, "AG-UI-Thread nicht gefunden.")
                    try:
                        target.cancel_run(thread_id=thread_id, run_id=run_id)
                    except RuntimeError as exc:
                        return error(409, str(exc))
                    return JSONResponse({"status": "cancelling"}, status_code=202)
                incoming = parse_run_input(body)
        except ValueError as exc:
            return error(400, str(exc))
        accept = request.headers.get("accept", "")
        if "text/event-stream" not in accept and "*/*" not in accept:
            return error(406, "Accept: text/event-stream erforderlich.")

        browser = (
            ui is not None and request.headers.get("x-cli-agent-browser") == "1"
        )
        if browser:
            assert ui is not None
            if not ui["registry"].is_owner(
                incoming.thread_id, request.headers.get("x-cli-agent-lease")
            ):
                return error(403, "Keine gültige Browser-Bindung für diesen Thread.")
            session = service.sessions.get(incoming.thread_id)
            if session is None:
                return error(404, "Session nicht gefunden.")
        else:
            try:
                session = await service.resolve_agui_thread(incoming.thread_id)
            except OverflowError:
                return error(429, "Zu viele AG-UI-Threads.")
            except Exception:
                return error(503, "Thread konnte nicht erstellt werden.")

        # Interrupted AG-UI runs have already CLOSED their SSE stream;
        # they must not consume one of the browser's HTTP/1.1 connection
        # reservations. A resume reserves a new streaming slot just like
        # an ordinary run, however, and must not bypass the limit.
        if browser and sum(
            other._busy and other._run is not None and other._run.agui
            and other._run.status != "interrupted"
            for other in service.sessions.values()
        ) >= MAX_CONCURRENT_BROWSER_RUNS:
            return error(429, "Maximal vier parallele Web-UI-Läufe erlaubt.")

        from cli_agent.interfaces.ag_ui_frontend_tools import (
            normalize_frontend_tools, checked_frontend_results,
            history_for_model,
        )
        from cli_agent.state.evidence_tools import PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME
        from cli_agent.platform.artifact_publication import PUBLISH_TOOL_NAME
        # Internal agent tools are not routed MCP tools and are deliberately
        # omitted from tool_states_snapshot(). Frontend clients must never
        # shadow them, even before any external tool has been enabled.
        reserved_names = {"agent__start_sub_run", PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, PUBLISH_TOOL_NAME}
        reserved_names.update(getattr(session.agent, "_tool_routes", {}))
        if hasattr(session.agent, "tool_states_snapshot"):
            reserved_names.update(
                item["name"] for item in session.agent.tool_states_snapshot()
            )
        try:
            frontend_tools = normalize_frontend_tools(
                incoming.tools, server_names=reserved_names
            )
        except ValueError as exc:
            return error(400, str(exc))

        # Only plain user/assistant messages are submitted to the model's
        # conversation history. AG-UI system/developer messages have no power
        # to modify cli-agent's own system prompt or configured policy.
        if incoming.resume:
            if len(incoming.resume) != 1:
                return error(400, "Alle offenen Interrupts müssen beantwortet werden.")
            try:
                run = session.resume_agui(
                    thread_id=incoming.thread_id,
                    run_id=incoming.run_id,
                    response=incoming.resume[0],
                )
            except (ValueError, RuntimeError) as exc:
                return error(409, str(exc))
        else:
            if session._busy or session._pending_interrupt is not None:
                return error(409, "Thread ist beschäftigt oder wartet auf Resume.")
            pending = session._pending_frontend
            frontend_results = None
            if pending:
                if session._pending_frontend_thread_id != incoming.thread_id:
                    return error(409, "Frontend-Tool-Ergebnis gehört zu anderem Thread.")
                try:
                    frontend_results = checked_frontend_results(incoming.messages, pending)
                except ValueError as exc:
                    return error(409, str(exc))
                # The model resumes a *server-owned* checkpoint. Additional
                # user instructions in the tool-result request would not be
                # visible to that continuation. Reject them explicitly rather
                # than silently claiming they were accepted.
                accepted_users = {
                    message["id"]: message["content"]
                    for message in session.agui_messages
                    if message.get("role") == "user"
                }
                if any(
                    message.get("role") == "user" and (
                        message["id"] not in accepted_users
                        or message["content"] != accepted_users[message["id"]]
                    )
                    for message in incoming.messages
                ):
                    return error(
                        409,
                        "Neue oder geänderte Benutzernachrichten sind während "
                        "eines Frontend-Tool-Resumes nicht erlaubt.",
                    )
            elif not incoming.prompt.strip():
                return error(400, "Eine Benutzer-Nachricht ist erforderlich.")
            if incoming.prompt.strip().casefold() in {"exit", "quit"} and browser and not pending:
                return error(400, "Lokale Shutdown-Befehle gehören nicht zum AG-UI-Run.")
            history = history_for_model(incoming.messages)
            if not pending and history and history[-1] == {
                "role": "user", "content": incoming.prompt
            }:
                history.pop()
            if not pending and hasattr(session.agent, "history"):
                session.agent.history[:] = history
            try:
                run = session.submit(
                    incoming.prompt if not pending else "",
                    require_listener=True, agui=True,
                    thread_id=incoming.thread_id, run_id=incoming.run_id,
                    frontend_results=frontend_results,
                )
            except ValueError as exc:
                return error(409, str(exc))
            except RuntimeError:
                return error(409, "Thread ist beschäftigt.")
            session.agent._agui_frontend_tools = frontend_tools
            session.agui_messages = list(incoming.messages)
            session.agui_state = incoming.state
            session.agui_context = list(incoming.context)
            session.agent._agui_external_context = list(incoming.context)
            if pending:
                session._pending_frontend = None
                session._pending_frontend_thread_id = None
        lease = request.headers.get("x-cli-agent-lease")

        async def stream():
            cursor = 0
            session.subscribe(run)
            try:
                if run.resume_approval is not None:
                    # An active SSE listener must exist BEFORE approving.
                    # The broker validates the original write preview again
                    # against the current workspace (TOCTOU defense).
                    approval_id, decision = run.resume_approval
                    if not session.broker.resolve(approval_id, decision):
                        run.status = "failed"
                        run.emit({"type": "RUN_ERROR", "message": "Freigabe ist abgelaufen."})
                while True:
                    if browser and not ui["registry"].is_owner(
                        session.id, lease, refresh=False
                    ):
                        return
                    for sequence, event in tuple(run.events):
                        if sequence > cursor:
                            cursor = sequence
                            yield encode_sse(event)
                    if run.done and cursor >= run.next_sequence - 1:
                        return
                    run.changed.clear()
                    if run.next_sequence > cursor + 1 or run.done:
                        continue
                    try:
                        await asyncio.wait_for(run.changed.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        # Keep browser intermediaries from silently closing
                        # quiet runs without generating protocol events.
                        yield ": keep-alive\n\n"
            finally:
                session.unsubscribe(run)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                # Nonstandard management metadata for authenticated external
                # API clients only. Keep AG-UI event payloads interoperable.
                **({} if browser else {"X-Cli-Agent-Session-Id": session.id}),
            },
        )

    async def ui_reconnect_run(request: Request) -> Response:
        """Browser-only replay of an already-active run after tab reload.

        AG-UI 1.0 does not support SSE stream resumption; keep this
        optional, access-controlled browser extension, not a public run API.
        """
        assert ui is not None
        sid = request.path_params["session_id"]
        if not ui["registry"].is_owner(
            sid, request.headers.get("x-cli-agent-lease"),
        ):
            return error(403, "Keine gültige Session-Bindung.")
        session = service.sessions.get(sid)
        run = session.run(request.path_params["run_id"]) if session is not None else None
        if run is None or not run.agui:
            return error(404, "AG-UI-Run nicht gefunden.")
        try:
            last = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            return error(400, "Ungültige Event-ID.")
        if last < 0 or last >= run.next_sequence:
            return error(400, "Ungültige Event-ID.")
        if run.events and last < run.events[0][0] - 1:
            return error(410, "Event-History ist nicht mehr vollständig.")

        browser_lease = request.headers.get("x-cli-agent-lease")

        async def stream():
            cursor = last
            session.subscribe(run)
            try:
                while True:
                    # A request authorized when established must not keep an SSE
                    # subscription after its tab's exclusive lease has expired.
                    if not ui["registry"].is_owner(
                        session.id, browser_lease, refresh=False
                    ):
                        return
                    if run.events and cursor < run.events[0][0] - 1:
                        return
                    for seq, event in tuple(run.events):
                        if seq > cursor:
                            cursor = seq
                            # Replayed approval requests must remain actionable.
                            # Resolved/denied requests are historical audit
                            # events, not interactive dialogs.
                            if (event.get("type") == "CUSTOM"
                                    and event.get("name") == "approval_required"):
                                request_id = str(event.get("value", {}).get("id", ""))
                                if not session.broker.is_pending(request_id):
                                    continue
                            yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    # A fast agent can finish while the SSE generator is
                    # yielding its first event. Do not terminate until EVERY
                    # event emitted after the snapshot has been drained.
                    if run.done and cursor >= run.next_sequence - 1:
                        return
                    run.changed.clear()
                    # New events may have arrived while yield suspended us.
                    # Recheck after clear to avoid an indefinitely lost wakeup.
                    if run.next_sequence > cursor + 1 or run.done:
                        continue
                    try:
                        await asyncio.wait_for(run.changed.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
            finally:
                session.unsubscribe(run)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        agent = session.agent
        return JSONResponse({
            "tools": agent.tool_states_snapshot(),
            "contexts": agent.context_states_snapshot(),
            "okf": agent.okf_status_snapshot(),
        })

    async def update_context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        if session._busy:
            return error(409, "Änderungen während eines Runs sind nicht erlaubt.")
        try:
            async with checked_json(request) as body:
                if not body or set(body) - {"tools", "contexts", "okf"}:
                    return error(400, "Unbekannte Context-Felder.")
                tools = body.get("tools", {})
                contexts = body.get("contexts", {})
                if not isinstance(tools, dict) or not isinstance(contexts, dict):
                    return error(400, "Tools und Contexts müssen Objekte sein.")
                if any(not isinstance(v, bool) for v in [*tools.values(), *contexts.values()]):
                    return error(400, "Aktivierungszustände müssen boolesch sein.")
                agent = session.agent
                available_tools = {item["name"] for item in agent.tool_states_snapshot()}
                available_contexts = {item["id"] for item in agent.context_states_snapshot()}
                if set(tools) - available_tools or set(contexts) - available_contexts:
                    return error(400, "Unbekanntes Tool oder Context.")
                okf = body.get("okf")
                if "okf" in body and (not isinstance(okf, bool) or not agent.okf_status_snapshot()["configured"]):
                    return error(400, "OKF ist nicht konfiguriert oder Zustand ungültig.")
                # checked_json() awaits the request body. A run may have
                # started since the initial busy check above. No awaits between
                # this check and the synchronous state mutations.
                if session._busy or session._closed:
                    return error(409, "Änderungen während eines Runs sind nicht erlaubt.")
                for name, enabled in tools.items():
                    agent.set_tool_enabled(name, enabled=enabled)
                for context_id, enabled in contexts.items():
                    agent.set_context_enabled(context_id, enabled=enabled)
                if "okf" in body:
                    agent.set_okf_enabled(enabled=okf)
                return await context(request)
        except ValueError as exc:
            return error(400, str(exc))

    async def working_context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        agent = session.agent
        return JSONResponse({
            "messages": agent.working_messages_snapshot(),
            "sub_runs": agent.sub_run_snapshots() if callable(getattr(agent, "sub_run_snapshots", None)) else [],
            "tools": agent.tool_states_snapshot(),
            "contexts": agent.context_states_snapshot(),
            "okf": agent.okf_status_snapshot(),
        })

    async def reset_history(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        if session._busy or session._closed:
            return error(409, "Session ist beschäftigt oder geschlossen.")
        session.broker.deny_all()
        session.agent.reset_history()
        session.ui_messages.clear()
        session.agui_messages.clear()
        session._pending_frontend = None
        session._pending_frontend_thread_id = None
        if hasattr(session.agent, "_agui_pending_working_messages"):
            session.agent._agui_pending_working_messages = None
        return JSONResponse({"reset": True})

    async def usage(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        return JSONResponse({"text": session.agent.token_usage_text()})

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

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def authenticate(request: Request, call_next: Any) -> Response:
        # Reject rebinding / proxies even for public static assets.
        host = request.headers.get("host", "").split(":")[0].lower()
        if host != API_HOST or request.headers.get("x-forwarded-host") or request.headers.get("forwarded"):
            return error(403, "Unzulässiger Host.")
        # Use the ASGI-decoded path; request.url.path may treat decoded # or
        # ? characters as URL delimiters and truncate the real path.
        path = request.scope["path"]
        origin = request.headers.get("origin")
        if ui is not None and path in {"/", "/assets/app.css", "/assets/app.js"}:
            if request.method != "GET" or origin is not None:
                return error(403, "Unzulässige Browser-Anfrage.")
            return await call_next(request)
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        token = header[len(prefix):] if header.startswith(prefix) else ""
        browser = (
            ui is not None
            and secrets.compare_digest(token, ui["token"])
            and request.headers.get("x-cli-agent-browser") == "1"
            and ((origin is None or origin == ui["origin"])
                 if request.method in {"GET", "HEAD"}
                 else origin == ui["origin"])
            and (path.startswith("/ui/") or path.startswith("/sessions/")
                 or path.startswith("/ag-ui") or path.startswith("/threads/"))
        )
        if browser:
            if path.startswith("/threads/"):
                parts = path.split("/")
                thread_id = _artifact_thread_from_token(parts[2]) if len(parts) == 5 else None
                if (request.method != "GET" or thread_id is None
                        or parts[3] != "artifacts" or not parts[4]
                        or not ui["registry"].is_owner(
                            thread_id, request.headers.get("x-cli-agent-lease"))):
                    return error(403, "Keine gültige Tab-Bindung für den Download.")
            if path.startswith("/sessions/"):
                parts = path.split("/")
                if (len(parts) < 4 or request.method == "DELETE"
                        or not ui["registry"].is_owner(
                            parts[2], request.headers.get("x-cli-agent-lease"))):
                    return error(403, "Keine gültige Tab-Bindung für diese Session.")
            return await call_next(request)
        if origin is not None:
            return error(403, "Browser-Origin nicht zugelassen.")
        if not token or not secrets.compare_digest(token, service.token):
            return error(401, "Authentifizierung erforderlich.")
        return await call_next(request)

    app = Starlette(
        routes=([
            Route("/health", health, methods=["GET"]),
            Route("/ag-ui", ag_ui_run, methods=["POST"]),
            Route("/ag-ui/capabilities", ag_ui_capabilities, methods=["GET"]),
            Route("/threads/{thread_id}/artifacts/{artifact_id}", download_artifact, methods=["GET"]),
            Route("/threads/{thread_id:path}", delete_agui_thread, methods=["DELETE"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/context", context, methods=["GET"]),
            Route("/sessions/{session_id}/working-context", working_context, methods=["GET"]),
            Route("/sessions/{session_id}/reset", reset_history, methods=["POST"]),
            Route("/sessions/{session_id}/usage", usage, methods=["GET"]),
            Route("/sessions/{session_id}/context", update_context, methods=["PATCH"]),
        ] + ([
            Route("/", ui_index, methods=["GET"]),
            Route("/assets/app.css", ui_css, methods=["GET"]),
            Route("/assets/app.js", ui_js, methods=["GET"]),
            Route("/ui/bootstrap", ui_bootstrap, methods=["POST"]),
            Route("/ui/sessions", ui_sessions, methods=["GET"]),
            Route("/ui/sessions", ui_session_new, methods=["POST"]),
            Route("/ui/sessions/{session_id}/ticket", ui_session_ticket, methods=["POST"]),
            Route("/ui/sessions/{session_id}", ui_session_delete, methods=["DELETE"]),
            Route("/ui/release", ui_release, methods=["POST"]),
            Route("/ui/heartbeat", ui_heartbeat, methods=["POST"]),
            Route("/ui/reconnect/{session_id}/{run_id}", ui_reconnect_run, methods=["GET"]),
            Route("/ui/render", ui_render, methods=["POST"]),
            Route("/ui/command", ui_command, methods=["POST"]),
        ] if ui is not None else [])),
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=authenticate)],
    )
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-agent-api", description="Local-only Agent API")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--with-os-read", action="store_const", const="read", dest="os_access")
    access.add_argument("--with-os-write", action="store_const", const="write", dest="os_access")
    parser.add_argument("--with-mcp", action="append", default=[], metavar="NAME")
    parser.add_argument("--with-data", action="store_true")
    parser.add_argument("--exclude-path", action="append", type=Path, default=[])
    parser.add_argument("--approve-tool", action="append", default=[], metavar="TOOL", help="Pre-approve one exact exposed tool name for this process run; repeat for multiple tools.")
    return parser


def build_service(args: argparse.Namespace) -> AgentApiService:
    from cli_agent.settings.admin_config import load_admin_config
    from cli_agent.settings.config import McpServerConfig, default_config_file, load_config
    from cli_agent.platform.execution import (
        apply_workspace_access_override, apply_data_override,
        apply_named_mcp_overrides, build_preapproval_callback,
        resolve_excluded_paths,
    )
    from cli_agent.context.file_context import ContextFileCliAgent
    from cli_agent.models.model_factory import create_model_client
    from cli_agent.platform.os_operations import Workspace
    from cli_agent.platform.logging_setup import configure_logging

    if not 1 <= args.port <= 65535:
        raise ValueError("Port muss im Bereich 1–65535 liegen.")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Workspace existiert nicht.")
    if args.exclude_path and args.os_access not in {"read", "write"}:
        raise ValueError("--exclude-path benötigt OS-Zugriff.")

    config_file = args.config or default_config_file()
    config = load_config(args.config)
    admin = load_admin_config()
    if args.model is not None:
        from dataclasses import replace
        config = replace(config, model=replace(config.model, model=args.model))
    exclusions = resolve_excluded_paths(workspace, tuple(args.exclude_path))
    config = apply_workspace_access_override(
        config, workspace_access=args.os_access, excluded_paths=exclusions
    )
    config = apply_data_override(
        config, enabled=args.with_data,
        workspace_access=args.os_access, excluded_paths=exclusions
    )
    config = apply_named_mcp_overrides(
        config, admin_config=admin, server_names=args.with_mcp,
        workspace_access=args.os_access, workspace=workspace,
    )
    configure_logging(config.logging)
    approval_workspace = None
    if args.os_access == "write":
        approval_workspace = Workspace.from_directory(
            workspace, McpServerConfig(name="os", config={"allow_write_files": True}),
            protected_paths=(config_file, *exclusions),
        )

    def factory(session: AgentSession):
        model_client = create_model_client(
            config.model, network=admin.network,
            credential_rules=admin.model_credentials,
        )
        agent = ContextFileCliAgent(
            workspace, model_client, config.mcp_servers,
            logging_config=config.logging,
            agent_timezone=config.agent.timezone,
            config_file=config_file,
            # API sessions never write LLM context dumps, even when enabled
            # in config.toml for CLI or flow execution.
            dump_llm_context=False,
            network=admin.network,
            web_providers=admin.web.providers,
            mcp_policy=admin.mcp,
            approval_callback=build_preapproval_callback(
                getattr(args, "approve_tool", ()) or (),
                fallback=session.approve,
            ),
            user_message_callback=session.broker.show_mcp_user_message,
            okf=config.okf,
            knowledge_vector=config.knowledge_vector,
            model_credential_rules=admin.model_credentials,
            workspace_access=args.os_access,
        )
        agent.event_callback = session.on_agent_event
        session.broker.set_event_sender(session._send_user_message)
        return agent

    return AgentApiService(factory, approval_workspace=approval_workspace)


def main() -> None:
    import uvicorn
    args = build_parser().parse_args()
    service = build_service(args)
    # Service managers and desktop integrations often pipe stdout. Explicitly
    # flush before Uvicorn blocks so clients can read the API credential.
    print(f"cli-agent-api: http://{API_HOST}:{args.port} (nur localhost)", flush=True)
    print(f"Bearer-Token: {service.token}", flush=True)
    uvicorn.run(
        create_app(service), host=API_HOST, port=args.port,
        proxy_headers=False, access_log=False, server_header=False,
    )
