# Configuration and administrator settings

`config.py` defines user-facing runtime configuration, and `admin_config.py` defines administrator-controlled security and policy settings. The bundled `config.example.toml` is a resource in this package. Configuration must not bypass runtime security enforcement; CLI/API and MCP servers share the same validation semantics.
