# Web UI customization reference

This page documents the embedded browser UI's configuration and all 26
Jinja2 extension blocks. The parent template is
`src/cli_agent/interfaces/templates/cli_agent/index.html`.

## Project configuration

Create `.cli-agent/project.toml` in the project workspace:

```toml
[ui]
title = "SignalDeck AI"
icon = "⚡"
default_language = "de"
theme = "dark"
template = "ui/custom.html"
stylesheets = ["ui/branding.css", "ui/layout.css"]

[ui.buttons]
new_session = true
list_sessions = true
reset_history = false
show_working_context = true
end_session = true
language_selector = false

[ui.buttons.commands]
tokens = true
add_web_context = false
clear_web_context = false
enable = true
disable = true
```

All settings are optional. Missing fields inherit global configuration or
defaults. `theme` accepts `bright` (default) or `dark`. `icon` defaults
to the CSS-drawn logo; `icon = ""` hides it, and a short symbol replaces it.
The supported languages are `en` and `de`; when the selector is visible a
saved selection overrides the default, while hiding it enforces the default.

CSS files are loaded in order: built-in `app.css`, selected theme, then
each item in `stylesheets`. CSS paths are direct children of
`.cli-agent/ui/` (up to eight files, 128 KiB each and 512 KiB total).
A Jinja template must also be a direct child of `.cli-agent/ui/`.

## All 26 Jinja2 extension blocks

```text
cli_agent/index.html
├── extra_head
├── header_row
│   ├── app_info
│   │   ├── app_icon
│   │   ├── app_title
│   │   └── session_meta
│   └── header_buttons
│       ├── button_language_selector
│       ├── button_new_session
│       ├── button_list_sessions
│       ├── button_reset_history
│       ├── button_show_working_context
│       └── button_end_session
├── chat
├── commands
│   ├── command_tokens
│   ├── command_add_web_context
│   ├── command_clear_web_context
│   ├── command_enable
│   └── command_disable
├── composer
├── dialogs
│   ├── sessions_dialog
│   ├── working_context_dialog
│   └── approval_dialog
└── footer
```

| Block | Purpose |
| --- | --- |
| `extra_head` | Additional HTML head markup |
| `header_row` | Complete app header |
| `app_info` | Logo, title, model/workspace/MCP metadata |
| `app_icon` | CSS logo or project symbol |
| `app_title` | Application heading |
| `session_meta` | Dynamic model/workspace/MCP description |
| `header_buttons` | Header controls container |
| `button_language_selector` | Language selector |
| `button_new_session` | Open a new session |
| `button_list_sessions` | List existing sessions |
| `button_reset_history` | Reset conversation |
| `button_show_working_context` | Inspect LLM context |
| `button_end_session` | End current session |
| `chat` | Conversation and tool events |
| `commands` | Local command toolbar |
| `command_tokens` | Token usage |
| `command_add_web_context` | Add web context |
| `command_clear_web_context` | Clear web context |
| `command_enable` | Enable MCP |
| `command_disable` | Disable MCP |
| `composer` | Prompt and Send/Stop controls |
| `dialogs` | All dialogs |
| `sessions_dialog` | Session management |
| `working_context_dialog` | Context inspector |
| `approval_dialog` | Tool approval |
| `footer` | Empty extension point |

## Inheritance example

Place this in `.cli-agent/ui/custom.html`:

```jinja2
{% extends "cli_agent/index.html" %}

{% block app_info %}
<div class="app-brand">
  {{ self.app_icon() }}
  <div class="app-brand-copy">
    {{ self.app_title() }}
    <p>Project dashboard</p>
    {{ self.session_meta() }}
  </div>
</div>
{% endblock %}

{% block header_buttons %}
<div class="header-actions">
  {{ self.button_show_working_context() }}
  <span>Custom actions</span>
  {{ self.button_new_session() }}
  {{ self.button_language_selector() }}
  {{ self.button_list_sessions() }}
  {{ self.button_reset_history() }}
  {{ self.button_end_session() }}
</div>
{% endblock %}
```

Blocks are nested; overriding a child alone leaves the parent unchanged.
When replacing a parent such as `app_info`, explicitly render any nested
children you still need with `{{ self.app_icon() }}` etc. Use
`{{ super() }}` to retain the parent block's complete contents.

To use a PNG, JPEG or WebP image directly under `.cli-agent/ui/`, set
`[ui] icon = "ui/logo.png"`. The image is limited to 1 MiB, validated
at startup and served only at `/assets/ui/icon/logo.png`. Other files
in the directory are not exposed. SVG and external image URLs are not
supported. The CSP permits only local same-origin images. Restart the UI
after changing the file.

The default icon is a CSS-drawn shape. Override only `app_icon` for a
different icon, for example:

```jinja2
{% block app_icon %}
<span class="app-icon app-icon-custom" aria-hidden="true">⚡</span>
{% endblock %}
```

## Compatibility and safety

The JavaScript relies on stable elements and IDs in the HTML.
Overrides must preserve those DOM nodes exactly once, even for hidden
controls. In particular, keep `session-meta`, `chat`, `composer`,
`prompt`, `send`, `stop-run`, `commands`, the header controls, all
command buttons and the dialog elements. The renderer validates
required IDs, commands, and script tags but cannot verify visual layout.

Template and CSS files are trusted local configuration, not LLM input.
They are read at UI startup with path/link restrictions and are not exposed
via the workspace MCP. The existing localhost security checks and CSP
still apply; inline scripts and external image URLs are not permitted.
Visibility switches are not server-side authorization controls.
Restart the UI after editing custom files.
