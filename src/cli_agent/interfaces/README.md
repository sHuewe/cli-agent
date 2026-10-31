# Interface adapters

Web UI routing, sessions, AG-UI protocol/transport, approval rendering and terminal formatting. These adapters present agent lifecycle, progress and tool calls without granting permissions. API/Web UI code must use runtime authorization and maintain isolation between sessions. Historic imports alias the implementation modules to avoid breaking monkeypatches and direct integrations.
