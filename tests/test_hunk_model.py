import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.hunk_model import (
    ChangeKind,
    attach_enclosing_context,
    diff_files,
    parse_unified_diff,
    split_oversized_hunk,
)


def test_modified_file_produces_one_hunk_for_a_small_change():
    old = "a = 1\nb = 2\nc = 3\n"
    new = "a = 1\nb = 20\nc = 3\n"
    hunks = diff_files("f.py", "f.py", old, new)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.change_kind == ChangeKind.MODIFIED
    assert h.added_lines == ["b = 20"]
    assert h.removed_lines == ["b = 2"]


def test_deletion_only_hunk_is_flagged():
    old = "a = 1\ntry:\n    risky()\nfinally:\n    cleanup()\n"
    new = "a = 1\ntry:\n    risky()\n"
    hunks = diff_files("f.py", "f.py", old, new)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.is_deletion_only
    assert h.added_lines == []
    assert any("cleanup()" in l for l in h.removed_lines)


def test_whitespace_only_hunk_detected():
    old = "def f():\n    x = 1\n    return x\n"
    new = "def f():\n    x = 1  \n    return x\n"  # trailing space only
    hunks = diff_files("f.py", "f.py", old, new)
    assert len(hunks) == 1
    assert hunks[0].is_whitespace_only


def test_no_content_change_rename_yields_no_hunks():
    content = "a = 1\n"
    hunks = diff_files("old.py", "new.py", content, content)
    assert hunks == []


def test_added_file_is_added_change_kind():
    hunks = diff_files(None, "new.py", None, "a = 1\nb = 2\n")
    assert len(hunks) == 1
    assert hunks[0].change_kind == ChangeKind.ADDED
    assert hunks[0].removed_lines == []


def test_deleted_file_is_deleted_change_kind():
    hunks = diff_files("old.py", None, "a = 1\nb = 2\n", None)
    assert len(hunks) == 1
    assert hunks[0].change_kind == ChangeKind.DELETED
    assert hunks[0].added_lines == []


def test_enclosing_context_extraction_finds_containing_function():
    old = (
        "def outer():\n"
        "    pass\n"
        "\n"
        "def get_user_orders(request):\n"
        "    user_id = request.args.get('user_id')\n"
        "    query = 'SELECT * FROM orders WHERE user_id = %s'\n"
        "    return db.execute(query, (user_id,))\n"
    )
    post_source = (
        "def outer():\n"
        "    pass\n"
        "\n"
        "def get_user_orders(request):\n"
        "    user_id = request.args.get('user_id')\n"
        "    query = f'SELECT * FROM orders WHERE user_id = {user_id}'\n"
        "    return db.execute(query)\n"
    )
    hunks = diff_files("app.py", "app.py", old, post_source)
    assert len(hunks) == 1
    h = hunks[0]
    attach_enclosing_context(h, post_source)
    assert h.enclosing_symbol == "get_user_orders"
    assert "db.execute(query)" in h.enclosing_context


def test_deleted_file_has_no_enclosing_context_lookup():
    hunks = diff_files("old.py", None, "def f():\n    pass\n", None)
    h = hunks[0]
    attach_enclosing_context(h, "")  # should not raise, no post-change source
    assert h.enclosing_symbol is None


def test_split_oversized_hunk_keeps_all_lines_and_shares_group_id():
    old_lines = "\n".join(f"x{i} = {i}" for i in range(300))
    new_lines = "\n".join(f"x{i} = {i + 1}" for i in range(300))
    hunks = diff_files("big.py", "big.py", old_lines + "\n", new_lines + "\n", context=0)
    assert len(hunks) == 1
    big = hunks[0]
    assert len(big.lines) > 200
    parts = split_oversized_hunk(big)
    assert len(parts) > 1
    assert all(p.hunk_group_id == parts[0].hunk_group_id for p in parts)
    total_lines = sum(len(p.lines) for p in parts)
    assert total_lines == len(big.lines)


def test_split_not_needed_for_small_hunk():
    old, new = "a = 1\n", "a = 2\n"
    hunks = diff_files("f.py", "f.py", old, new)
    parts = split_oversized_hunk(hunks[0])
    assert parts == [hunks[0]]


def test_parse_unified_diff_multi_file():
    diff_text = (
        "diff --git a/a.py b/a.py\n"
        "index 111..222 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        " x = 1\n"
        "-y = 2\n"
        "+y = 3\n"
        "diff --git a/b.py b/b.py\n"
        "index 333..444 100644\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-z = 0\n"
        "+z = 1\n"
    )
    hunks = parse_unified_diff(diff_text)
    files = {h.file_path for h in hunks}
    assert files == {"a.py", "b.py"}
    assert len(hunks) == 2
