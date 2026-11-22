"""Publish a named OKF tree without changing model checkpoints or staging files."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlsplit

import yaml

MAX_FILES = 100_000
LINK_RE = re.compile(
    r"(?P<inline_prefix>\]\(\s*)(?P<inline_url><[^>\n]+>|[^\s()]+)"
    r"|(?P<ref_prefix>^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)(?P<ref_url><[^>\n]+>|[^\s]+)"
    r"|(?P<html_prefix>\b(?:href|src)\s*=\s*['\"])(?P<html_url>[^'\"\n]+)",
    re.MULTILINE | re.IGNORECASE,
)
CODE_SPAN_RE = re.compile(r"(?P<ticks>\x60+)[^\x60]*?(?P=ticks)")
FENCE_RE = re.compile(r"^[ ]{0,3}(?P<fence>\x60{3,}|~{3,})")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)},
                    *{f"lpt{i}" for i in range(1, 10)}}


def _relative_root(workspace: Path, value: str, *, name: str) -> Path:
    if not isinstance(value, str) or not value or chr(92) in value:
        raise ValueError(f"Invalid {name}: {value!r}")
    unix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (unix.is_absolute() or windows.is_absolute() or windows.drive
            or any(x in ("..", ".") for x in value.split("/"))
            or any(not x for x in value.split("/"))):
        raise ValueError(f"Unsafe {name}: {value!r}")
    root = workspace.absolute()
    path = root / Path(*unix.parts)
    current = root
    if root.is_symlink() or root.resolve(strict=False) != root:
        raise ValueError(f"Symlinked workspace: {root}")
    for part in unix.parts:
        current /= part
        if current.is_symlink() or (
            callable(getattr(current, "is_junction", None)) and current.is_junction()
        ):
            raise ValueError(f"Symlink or junction in {name}: {current}")
    return path


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Required publication source is not a directory: {root}")
    found: dict[str, Path] = {}
    casefolded: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        directory = Path(current)
        for entry in [*directories, *files]:
            path = directory / entry
            if path.is_symlink() or (
                callable(getattr(path, "is_junction", None)) and path.is_junction()
            ):
                raise ValueError(f"Symlink/junction not allowed: {path}")
        for name in files:
            file = directory / name
            if not file.is_file() or file.stat().st_nlink > 1:
                raise ValueError(f"Non-regular/shared publication input: {file}")
            relative = file.relative_to(root).as_posix()
            if relative.casefold() in casefolded:
                raise ValueError(f"Case-insensitive file collision: {relative}")
            casefolded.add(relative.casefold())
            found[relative] = file
            if len(found) > MAX_FILES:
                raise ValueError("Too many files for named OKF publication.")
    return found


def _slug(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Concept title is missing.")
    de = str.maketrans({"Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
    normalized = unicodedata.normalize("NFKD", title.translate(de))
    ascii_title = normalized.encode("ascii", errors="ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    result = result[:72].rstrip("-")
    if not result or result in {"index", "log", *WINDOWS_RESERVED}:
        raise ValueError(f"No usable descriptive filename for title: {title!r}")
    return result


def _frontmatter_title(path: Path) -> str | None:
    with path.open("r", encoding="utf-8") as reader:
        if reader.readline().strip() != "---":
            return None
        block = []
        for _ in range(100):
            line = reader.readline()
            if not line:
                break
            if line.strip() == "---":
                document = yaml.safe_load("".join(block))
                if isinstance(document, dict) and isinstance(document.get("title"), str):
                    return document["title"].strip() or None
                return None
            block.append(line)
    return None


def _concept_mapping(
    generated: dict[str, Path], state_root: Path,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    if not state_root.is_dir():
        raise ValueError(f"State directory is unavailable: {state_root}")
    planned: list[tuple[str, str, str]] = []
    for doc in sorted(state_root.iterdir()):
        if doc.is_symlink() or (
            callable(getattr(doc, "is_junction", None)) and doc.is_junction()
        ):
            raise ValueError(f"Unsafe state directory: {doc}")
        if not doc.is_dir():
            continue
        group = doc / "group.json"
        if group.is_symlink() or (
            callable(getattr(group, "is_junction", None)) and group.is_junction()
        ):
            raise ValueError(f"Unsafe group checkpoint: {group}")
        if not group.is_file():
            continue
        data = json.loads(group.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("concepts"), list):
            raise ValueError(f"Malformed group checkpoint: {group}")
        for concept in data["concepts"]:
            if not isinstance(concept, dict):
                raise ValueError(f"Malformed concept in {group}")
            path, cid, title = (
                concept.get("path"), concept.get("id"), concept.get("title"),
            )
            if not all(isinstance(value, str) and value.strip()
                       for value in (path, cid, title)):
                raise ValueError(f"Missing concept identity/title in {group}")
            parts = PurePosixPath(path)
            if (parts.is_absolute() or len(parts.parts) < 3
                    or parts.parts[:2] != ("doctrines", doc.name)
                    or any(x in ("..", ".") for x in path.split("/"))
                    or chr(92) in path or parts.suffix.lower() != ".md"
                    or parts.name.casefold() in {"index.md", "log.md"}):
                raise ValueError(f"Unsafe concept path: {path!r}")
            if path not in generated:
                raise ValueError(f"Planned concept file is missing: {path}")
            title = _frontmatter_title(generated[path]) or title
            planned.append((path, cid, _slug(title)))
    if not planned:
        raise ValueError("No generated concept files found in group checkpoints.")
    originals: set[str] = set()
    for path, _, _ in planned:
        if path.casefold() in originals:
            raise ValueError(f"Duplicate planned concept path: {path}")
        originals.add(path.casefold())
    used = {path.casefold() for path in generated if path.casefold() not in originals}
    mapping: dict[str, str] = {}
    report: list[dict[str, str]] = []
    for path, cid, name in sorted(planned):
        parent = posixpath.dirname(path)
        target = f"{parent}/{name}.md"
        if target.casefold() in used:
            safe_id = re.sub(r"[^a-z0-9-]+", "-", cid.lower()).strip("-")
            if not safe_id:
                raise ValueError(f"Unsafe concept ID: {cid!r}")
            target = f"{parent}/{name[:56].rstrip('-')}-{safe_id[:24]}.md"
        if target.casefold() in used:
            raise ValueError(f"Unresolvable filename collision: {target}")
        used.add(target.casefold())
        mapping[path] = target
        report.append({"concept_id": cid, "original": path, "published": target})
    return mapping, report


def _rewrite_link(
    url: str, *, source_file: str, published_file: str,
    mapping: dict[str, str], originals: set[str], original_root: str,
) -> str:
    angled = url.startswith("<") and url.endswith(">")
    raw = url[1:-1] if angled else url
    parsed = urlsplit(raw)
    if (parsed.scheme or parsed.netloc or not parsed.path
            or parsed.path.startswith("/") or chr(92) in parsed.path):
        return url
    path = unquote(parsed.path)
    if any(ord(char) < 32 for char in path):
        raise ValueError(f"Unsafe Markdown link in {source_file}: {url!r}")
    original = posixpath.normpath(
        posixpath.join(posixpath.dirname(source_file), path)
    )
    if original not in originals:
        root_prefix = original_root.strip("/") + "/"
        if path.startswith(root_prefix) and path[len(root_prefix):] in originals:
            original = path[len(root_prefix):]
        elif path in originals and path.startswith(("doctrines/", "themen/")):
            original = path
    if original == ".." or original.startswith("../"):
        raise ValueError(
            f"Markdown link escapes the published root in {source_file}: {url!r}"
        )
    if original not in originals:
        if path.lower().endswith(".md"):
            raise ValueError(
                f"Broken internal Markdown link in {source_file}: {url!r}"
            )
        return url
    new_target = mapping.get(original, original)
    new_rel = posixpath.relpath(new_target, posixpath.dirname(published_file) or ".")
    fixed = quote(new_rel, safe="/-._~")
    if parsed.query:
        fixed += "?" + parsed.query
    if parsed.fragment:
        fixed += "#" + parsed.fragment
    return f"<{fixed}>" if angled else fixed


def _rewrite_markdown(
    text: str, *, old: str, new: str, mapping: dict[str, str],
    originals: set[str], original_root: str,
) -> str:
    """Rewrite full Markdown link constructs, including multiline links.

    Protect fenced and inline code from replacement. Parse contiguous prose
    across newlines so a link like [Concept](\nold-name.md\n) is not missed.
    """
    def rewrite_prose(prose: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            prefix = (
                match.group("inline_prefix") or match.group("ref_prefix")
                or match.group("html_prefix")
            )
            url = (
                match.group("inline_url") or match.group("ref_url")
                or match.group("html_url")
            )
            return prefix + _rewrite_link(
                url, source_file=old, published_file=new,
                mapping=mapping, originals=originals, original_root=original_root,
            )

        chunks: list[str] = []
        cursor = 0
        for code in CODE_SPAN_RE.finditer(prose):
            chunks.append(LINK_RE.sub(substitute, prose[cursor:code.start()]))
            chunks.append(code.group(0))
            cursor = code.end()
        chunks.append(LINK_RE.sub(substitute, prose[cursor:]))
        return "".join(chunks)

    fenced: tuple[str, int] | None = None
    rewritten: list[str] = []
    prose: list[str] = []
    for line in text.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if match is not None:
            marker = match.group("fence")
            if fenced is None:
                rewritten.append(rewrite_prose("".join(prose)))
                prose.clear()
                fenced = (marker[0], len(marker))
                rewritten.append(line)
                continue
            if fenced[0] == marker[0] and len(marker) >= fenced[1]:
                fenced = None
            rewritten.append(line)
        elif fenced is None:
            prose.append(line)
        else:
            rewritten.append(line)
    rewritten.append(rewrite_prose("".join(prose)))
    return "".join(rewritten)

def publish_named_okf(
    *, workspace: Path, source_root: str,
    destination_root: str, state_root: str,
) -> dict:
    """Create and atomically swap a complete named publication."""
    workspace = workspace.resolve()
    src = _relative_root(workspace, source_root, name="source_root")
    dst = _relative_root(workspace, destination_root, name="destination_root")
    state = _relative_root(workspace, state_root, name="state_root")
    if (src == dst or src in dst.parents or dst in src.parents
            or state == dst or state in dst.parents or dst in state.parents):
        raise ValueError("Publication directories must not overlap.")
    originals = _files(src)
    mapping, report = _concept_mapping(originals, state)
    published_paths = {mapping.get(path, path) for path in originals}
    if len(published_paths) != len(originals):
        raise ValueError("Destination filenames are not unique.")
    if "concept-path-map.json" in published_paths:
        raise ValueError("Mapping output conflicts with generated file.")
    backup = dst.with_name("." + dst.name + ".previous")
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"Previous publication backup exists: {backup}")
    if dst.exists():
        _files(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + dst.name + ".staging-", dir=dst.parent))
    published_markdown = 0
    try:
        for old_path, original in sorted(originals.items()):
            target = staging / Path(*PurePosixPath(mapping.get(old_path, old_path)).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if old_path.lower().endswith(".md"):
                new_text = _rewrite_markdown(
                    original.read_text(encoding="utf-8"),
                    old=old_path, new=mapping.get(old_path, old_path),
                    mapping=mapping, originals=set(originals),
                    original_root=source_root,
                )
                target.write_text(new_text, encoding="utf-8")
                published_markdown += 1
            else:
                shutil.copyfile(original, target)
        (staging / "concept-path-map.json").write_text(
            json.dumps({"status": "ok", "concepts": report},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if set(_files(staging)) != published_paths | {"concept-path-map.json"}:
            raise ValueError("Publication is incomplete.")
        moved_existing = False
        if dst.exists():
            os.replace(dst, backup)
            moved_existing = True
        try:
            os.replace(staging, dst)
        except BaseException:
            if moved_existing:
                os.replace(backup, dst)
            raise
        if moved_existing:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "ok", "source_root": source_root,
        "published_root": destination_root,
        "concepts_renamed": sum(a != b for a, b in mapping.items()),
        "concept_count": len(mapping),
        "markdown_files_checked": published_markdown,
        "mapping": f"{destination_root}/concept-path-map.json",
    }
