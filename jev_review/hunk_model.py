"""Hunk model: split a diff (or a before/after file pair) into
independently-reviewable Hunk objects, and attach enclosing-function
context via the stdlib ``ast`` module.

See design-doc.md §1 for the rationale behind every choice here.
"""

from __future__ import annotations

import ast
import difflib
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum

# Size cap for what goes into a single Jev `state` (design-doc.md §1).
# Chosen to stay well under Jev's advertised 32k-token context while
# leaving headroom for enclosing-function context and question growth.
HUNK_MAX_LINES = 200
HUNK_MAX_CHARS = 6000

# Separate, larger cap for the LLM review prompt (not used until the
# review stage — kept here since it's a hunk-model-level constant).
LLM_PROMPT_MAX_CHARS = 12000

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class ChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    MODE_ONLY = "mode_only"


@dataclass(frozen=True)
class DiffLine:
    tag: str  # '+', '-', or ' '
    text: str


@dataclass
class Hunk:
    file_path: str
    change_kind: ChangeKind
    header: str
    old_range: tuple[int, int]
    new_range: tuple[int, int]
    lines: list[DiffLine]
    old_path: str | None = None
    enclosing_symbol: str | None = None
    enclosing_context: str | None = None
    context_truncated: bool = False
    is_binary: bool = False
    hunk_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    hunk_group_id: str | None = None

    @property
    def added_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.tag == "+"]

    @property
    def removed_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.tag == "-"]

    @property
    def is_deletion_only(self) -> bool:
        return len(self.added_lines) == 0 and len(self.removed_lines) > 0

    @property
    def is_whitespace_only(self) -> bool:
        # Order-preserving comparison: reordered lines with identical
        # stripped content (e.g. two statements swapped) are a real
        # logic change, not a whitespace-only edit, even though a
        # sorted-set comparison would call them equal.
        added = [l.strip() for l in self.added_lines]
        removed = [l.strip() for l in self.removed_lines]
        return added == removed and len(added) > 0

    @property
    def rendered_diff(self) -> str:
        out = [self.header]
        for l in self.lines:
            out.append(f"{l.tag}{l.text}")
        return "\n".join(out)

    @property
    def char_size(self) -> int:
        return len(self.rendered_diff)

    def render_state(self, llm: bool = False) -> str:
        """Render the text sent to Jev (llm=False) or the review LLM
        (llm=True, larger cap, no truncation marker games)."""
        cap = LLM_PROMPT_MAX_CHARS if llm else HUNK_MAX_CHARS
        parts = [f"FILE: {self.file_path}", f"CHANGE: {self.change_kind.value}"]
        if self.enclosing_symbol:
            parts.append(f"ENCLOSING FUNCTION (post-change): {self.enclosing_symbol}")
            if self.enclosing_context:
                parts.append(self.enclosing_context)
                if self.context_truncated:
                    parts.append("[enclosing context truncated to fit size cap]")
        parts.append("DIFF:")
        parts.append(self.rendered_diff)
        state = "\n".join(parts)
        if len(state) > cap:
            state = state[:cap] + f"\n[...truncated {len(state) - cap} chars...]"
        return state


def _build_hunks_from_unified(
    file_path: str,
    old_path: str | None,
    change_kind: ChangeKind,
    diff_lines: list[str],
) -> list[Hunk]:
    """Parse the hunk (@@ ... @@) blocks out of a unified-diff body for
    one file, already stripped of the `--- a/...` / `+++ b/...` header
    lines."""
    hunks: list[Hunk] = []
    i = 0
    while i < len(diff_lines):
        m = _HUNK_HEADER_RE.match(diff_lines[i])
        if not m:
            i += 1
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2) or "1")
        new_start = int(m.group(3))
        new_count = int(m.group(4) or "1")
        header = diff_lines[i]
        i += 1
        body: list[DiffLine] = []
        while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
            line = diff_lines[i]
            if line == "":
                i += 1
                continue
            tag, text = line[0], line[1:]
            if tag in ("+", "-", " "):
                body.append(DiffLine(tag=tag, text=text))
            i += 1
        hunks.append(
            Hunk(
                file_path=file_path,
                old_path=old_path,
                change_kind=change_kind,
                header=header,
                old_range=(old_start, old_count),
                new_range=(new_start, new_count),
                lines=body,
            )
        )
    return hunks


def diff_files(
    old_path: str | None,
    new_path: str | None,
    old_content: str | None,
    new_content: str | None,
    *,
    context: int = 3,
) -> list[Hunk]:
    """Diff two versions of one file (either side may be None/empty for
    an added or deleted file) into Hunks, using the same splitter path
    real PR diffs and eval before/after pairs both go through.
    """
    if old_content is None and new_content is None:
        return []

    display_path = new_path or old_path
    assert display_path is not None

    if old_content is None:
        change_kind = ChangeKind.ADDED
        old_lines: list[str] = []
    elif new_content is None:
        change_kind = ChangeKind.DELETED
        old_lines = old_content.splitlines()
    else:
        change_kind = ChangeKind.MODIFIED
        old_lines = old_content.splitlines()

    new_lines = (new_content or "").splitlines()

    if old_content is not None and new_content is not None and old_content == new_content:
        # No content change: pure rename or mode-only change, handled by
        # the caller/pre-filter, not represented as a hunk.
        return []

    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            lineterm="",
            n=context,
        )
    )
    # Drop the '--- '/'+++ ' header lines difflib prepends.
    body = [l for l in diff if not (l.startswith("--- ") or l.startswith("+++ "))]

    if old_path and new_path and old_path != new_path:
        kind = ChangeKind.RENAMED
    else:
        kind = change_kind

    hunks = _build_hunks_from_unified(display_path, old_path if kind == ChangeKind.RENAMED else None, kind, body)
    return hunks


def parse_unified_diff(diff_text: str) -> list[Hunk]:
    """Parse a real `git diff`-style unified diff (possibly multiple
    files) into Hunks. Renames/mode-only changes with no hunk body
    produce no Hunks here — the caller's pre-filter step logs those
    separately as no-content-change."""
    hunks: list[Hunk] = []
    lines = diff_text.splitlines()
    i = 0
    current_file: str | None = None
    current_old_path: str | None = None
    file_block: list[str] = []
    is_rename = False

    def flush() -> None:
        nonlocal file_block, current_file, current_old_path, is_rename
        if current_file and file_block:
            kind = ChangeKind.RENAMED if is_rename else ChangeKind.MODIFIED
            hunks.extend(
                _build_hunks_from_unified(
                    current_file, current_old_path if is_rename else None, kind, file_block
                )
            )
        file_block = []

    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            flush()
            current_file = None
            current_old_path = None
            is_rename = False
        elif line.startswith("rename from "):
            current_old_path = line[len("rename from ") :]
            is_rename = True
        elif line.startswith("--- "):
            pass
        elif line.startswith("+++ "):
            path = line[4:]
            if path.startswith("b/"):
                path = path[2:]
            current_file = path
        else:
            file_block.append(line)
        i += 1
    flush()
    return hunks


def attach_enclosing_context(hunk: Hunk, post_change_source: str) -> None:
    """Find the function/async-function enclosing the hunk's new-line
    range in the post-change file (stdlib `ast` — sufficient for this
    PoC per design-doc.md decision #2) and attach it. Truncates to
    signature+docstring+diff-adjacent lines if the whole function would
    blow the size cap, and sets `context_truncated` in that case."""
    if hunk.change_kind == ChangeKind.DELETED:
        return  # no post-change file to look in
    try:
        tree = ast.parse(post_change_source)
    except SyntaxError:
        return

    # Use the range of the actually-*changed* ('+') lines, not the full
    # context-padded new_range — a hunk's leading/trailing context lines
    # (e.g. a blank line just before a function starts) can fall outside
    # the enclosing function even though every changed line is inside it.
    new_start, _ = hunk.new_range
    line_no = new_start
    changed_lines: list[int] = []
    for l in hunk.lines:
        if l.tag == "+":
            changed_lines.append(line_no)
            line_no += 1
        elif l.tag == " ":
            line_no += 1
        # '-' lines don't consume a post-change line number.
    if changed_lines:
        start, end = min(changed_lines), max(changed_lines)
    else:
        start, end = hunk.new_range[0], hunk.new_range[0] + max(hunk.new_range[1], 1) - 1

    source_lines = post_change_source.splitlines()

    best: ast.AST | None = None
    best_span = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node_end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= start and end <= node_end:
                span = node_end - node.lineno
                if best_span is None or span < best_span:
                    best, best_span = node, span

    if best is None:
        return

    func_start = best.lineno
    func_end = getattr(best, "end_lineno", best.lineno)
    full_text = "\n".join(source_lines[func_start - 1 : func_end])
    hunk.enclosing_symbol = best.name

    if len(full_text) <= HUNK_MAX_CHARS // 2:
        hunk.enclosing_context = full_text
        return

    # Truncate: signature + docstring (if any) + a window around the
    # diff lines themselves.
    sig_end = best.body[0].lineno - 1 if best.body else best.lineno
    sig_lines = source_lines[func_start - 1 : sig_end]
    window_start = max(func_start, start - 10)
    window_end = min(func_end, end + 10)
    window_lines = source_lines[window_start - 1 : window_end]
    hunk.enclosing_context = "\n".join(sig_lines) + "\n    ...\n" + "\n".join(window_lines)
    hunk.context_truncated = True


def split_oversized_hunk(hunk: Hunk) -> list[Hunk]:
    """Split a hunk exceeding HUNK_MAX_LINES/HUNK_MAX_CHARS into
    sub-hunks sharing a `hunk_group_id`, splitting on the nearest
    context-line boundary. A single unsplittable oversized run is
    truncated in place (render_state already truncates on render;
    this function only handles splitting by line count)."""
    if len(hunk.lines) <= HUNK_MAX_LINES and hunk.char_size <= HUNK_MAX_CHARS:
        return [hunk]

    group_id = hunk.hunk_group_id or uuid.uuid4().hex[:12]
    chunks: list[list[DiffLine]] = []
    current: list[DiffLine] = []
    last_context_idx: int | None = None  # index within `current` of the last ' ' line
    for line in hunk.lines:
        current.append(line)
        if line.tag == " ":
            last_context_idx = len(current) - 1
        if len(current) >= HUNK_MAX_LINES:
            # Prefer splitting at the most recent context-line boundary
            # if there was one recently; otherwise hard-split at the cap
            # (e.g. a run of all-changed lines with no context at all).
            if last_context_idx is not None and last_context_idx >= len(current) - 20:
                split_at = last_context_idx + 1
            else:
                split_at = len(current)
            chunks.append(current[:split_at])
            current = current[split_at:]
            last_context_idx = None
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [hunk.lines]

    out = []
    old_ln, new_ln = hunk.old_range[0], hunk.new_range[0]
    for chunk in chunks:
        old_count = sum(1 for l in chunk if l.tag in ("-", " "))
        new_count = sum(1 for l in chunk if l.tag in ("+", " "))
        sub = Hunk(
            file_path=hunk.file_path,
            old_path=hunk.old_path,
            change_kind=hunk.change_kind,
            header=f"@@ -{old_ln},{old_count} +{new_ln},{new_count} @@",
            old_range=(old_ln, old_count),
            new_range=(new_ln, new_count),
            lines=chunk,
            enclosing_symbol=hunk.enclosing_symbol,
            enclosing_context=hunk.enclosing_context,
            context_truncated=hunk.context_truncated,
            hunk_group_id=group_id,
        )
        out.append(sub)
        old_ln += old_count
        new_ln += new_count
    return out
