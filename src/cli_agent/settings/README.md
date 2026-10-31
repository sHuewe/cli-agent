# Configuration and administration

`config.py` validates user configuration and `admin_config.py` restricts administrator-controlled access and network/credential policies. The packaged `config.example.toml` is read via `importlib.resources.files("cli_agent.settings")`.

Settings are inputs to runtime policy enforcement, not a substitute for it. All entry points should share the same validated configuration structures.
