"""Session, AG-UI and context endpoint handlers.

A handler factory captures the same request-validation and session state that
create_app supplies; HTTP routes are assembled in the transport entrypoint.
"""
from __future__ import annotations
import asyncio
import json
from typing import Any
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from cli_agent.interfaces.agent_api_sessions import MAX_CONCURRENT_BROWSER_RUNS, _artifact_thread_from_token


def build_api_handlers(service: Any, ui: dict[str, Any] | None, error: Any, checked_json: Any) -> dict[str, Any]:
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


    return {name: handler for name, handler in (
        ("create_session", create_session),
        ("delete_session", delete_session),
        ("delete_agui_thread", delete_agui_thread),
        ("download_artifact", download_artifact),
        ("ag_ui_capabilities", ag_ui_capabilities),
        ("ag_ui_run", ag_ui_run),
        ("ui_reconnect_run", ui_reconnect_run),
        ("context", context),
        ("update_context", update_context),
        ("working_context", working_context),
        ("reset_history", reset_history),
        ("usage", usage),
    )}
