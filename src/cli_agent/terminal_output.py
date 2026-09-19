from __future__ import annotations

# Unicode controls which can change the visual ordering of otherwise ordinary
# text. They are legitimate in bidirectional text, but unsafe at terminal
# trust boundaries where the exact identity of a tool, path, or action matters.
_BIDI_CONTROLS = frozenset(
    {
        0x061C,  # ARABIC LETTER MARK
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
    }
)


def _escaped_codepoint(codepoint: int) -> str:
    # Use Unicode escapes rather than \\xNN so the rendered representation is
    # also valid when it appears inside generated TOML basic strings.
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def sanitize_terminal_text(value: object, *, multiline: bool = True) -> str:
    """Neutralize terminal/display controls without restricting normal Unicode.

    Ordinary Unicode text, emoji, variation selectors and zero-width joiners are
    preserved. C0/C1 controls, carriage returns, Unicode line/paragraph
    separators and bidirectional formatting controls are rendered visibly as
    escapes instead of being interpreted by the terminal.

    With multiline=True, LF and TAB are preserved for normal prose. Security-
    sensitive identifiers should use multiline=False so they cannot create
    additional terminal lines or indentation.
    """

    text = str(value)
    result: list[str] = []

    for character in text:
        codepoint = ord(character)

        if character == "\n" and multiline:
            result.append(character)
            continue
        if character == "\t" and multiline:
            result.append(character)
            continue

        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in _BIDI_CONTROLS
            or codepoint in {0x2028, 0x2029}
        ):
            if character == "\n":
                result.append("\\n")
            elif character == "\r":
                result.append("\\r")
            elif character == "\t":
                result.append("\\t")
            else:
                result.append(_escaped_codepoint(codepoint))
            continue

        result.append(character)

    return "".join(result)
