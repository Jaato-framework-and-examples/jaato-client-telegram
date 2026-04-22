"""Tests for semantic markup rendering."""

from jaato_client_telegram.semantic_markup import (
    extract_semantic_blocks,
    render_j_code,
    render_j_table,
    render_nb_row,
    render_j_collapse,
    render_security_warning,
    render_semantic_markup,
)


class TestRenderJCode:
    def test_strips_jtok_wrappers(self):
        inner = '<j-line><j-tok t="k">def</j-tok> hello():</j-line>'
        result = render_j_code(inner)
        assert "<j-tok" not in result
        assert "def hello():" in result

    def test_line_numbers_gutter(self):
        inner = '<j-line n="1"><j-tok t="k">def</j-tok> hello():</j-line>\n<j-line n="2">    pass</j-line>'
        result = render_j_code(inner)
        assert "  1 | def hello():" in result
        assert "  2 | pass" in result

    def test_no_line_numbers(self):
        inner = '<j-line><j-tok t="k">def</j-tok> hello():</j-line>'
        result = render_j_code(inner)
        assert "def hello():" in result
        assert "|" not in result

    def test_wraps_in_pre_code(self):
        inner = '<j-line><j-tok t="k">print</j-tok>(42)</j-line>'
        result = render_j_code(inner)
        assert result.startswith("<pre><code>")
        assert result.endswith("</code></pre>")

    def test_empty_content(self):
        assert render_j_code("") == ""

    def test_server_entity_unescaping(self):
        inner = '<j-line><j-tok t="k">if</j-tok> x &lt; 10:</j-line>'
        result = render_j_code(inner)
        assert "if x < 10:" in result

    def test_plain_text_without_jline(self):
        inner = "print(42)"
        result = render_j_code(inner)
        assert "print(42)" in result


class TestRenderJTable:
    def test_basic_table(self):
        inner = ('<j-thead><j-th>Name</j-th><j-th>Age</j-th></j-thead>'
                  '<j-tr><j-td>Alice</j-td><j-td>30</j-td></j-tr>')
        result = render_j_table(inner)
        assert "<pre>" in result
        assert "Alice" in result
        assert "30" in result

    def test_pipe_separated(self):
        inner = '<j-thead><j-th>A</j-th></j-thead><j-tr><j-td>1</j-td></j-tr>'
        result = render_j_table(inner)
        assert "| A |" in result
        assert "| 1 |" in result

    def test_column_alignment(self):
        inner = ('<j-thead><j-th>Name</j-th></j-thead>'
                  '<j-tr><j-td>A</j-td></j-tr>')
        result = render_j_table(inner)
        # "Name" is wider than "A", so padding should align
        lines = result.strip("<pre></pre>").strip().split("\n")
        if len(lines) >= 2:
            assert lines[0].startswith("| Name")

    def test_empty_table(self):
        result = render_j_table("")
        assert "<pre>" in result

    def test_server_entity_unescaping(self):
        inner = '<j-thead><j-th>A &amp; B</j-th></j-thead><j-tr><j-td>1 &lt; 2</j-td></j-tr>'
        result = render_j_table(inner)
        # Server entities are unescaped then re-escaped for Telegram HTML
        assert "A &amp; B" in result
        assert "1 &lt; 2" in result
        # Verify raw server entities are not present unprocessed
        assert "A &amp;amp; B" not in result


class TestRenderNbRow:
    def test_input_row(self):
        result = render_nb_row("x = 1", "input", "In [3]:")
        assert "<pre><code>" in result
        assert "In [3]" in result
        assert "x = 1" in result

    def test_stdout_row(self):
        result = render_nb_row("42", "stdout", "Out [3]:")
        assert "<pre>" in result
        assert "Out [3]" in result
        assert "<code>" not in result  # stdout is not a code block

    def test_error_row(self):
        result = render_nb_row("error msg", "error", "Err [3]:")
        assert "<pre>" in result
        assert "Err [3]" in result

    def test_empty_content(self):
        assert render_nb_row("", "input", "In [1]:") == ""

    def test_emoji_fallback_no_label(self):
        result = render_nb_row("hello", "stdout", "")
        assert "\U0001f4e4" in result

    def test_embedded_semantic_blocks(self):
        inner = '<j-code language="python">\n<j-line>x = 1</j-line>\n</j-code>'
        result = render_nb_row(inner, "input", "In [1]:")
        assert "<pre><code>" in result


class TestRenderJCollapse:
    def test_basic_collapse(self):
        result = render_j_collapse("Hidden content")
        assert "<blockquote>Hidden content</blockquote>" == result

    def test_empty_content(self):
        assert render_j_collapse("") == ""

    def test_html_escaping(self):
        result = render_j_collapse("a < b & c > d")
        assert "&lt;" in result
        assert "&gt;" in result


class TestRenderSecurityWarning:
    def test_basic_warning(self):
        result = render_security_warning("Dangerous op")
        assert "<blockquote>" in result
        assert "\u26a0\ufe0f" in result
        assert "Dangerous op" in result

    def test_empty_content(self):
        assert render_security_warning("") == ""


class TestExtractSemanticBlocks:
    def test_text_only(self):
        parts = extract_semantic_blocks("Hello world")
        assert len(parts) == 1
        assert parts[0]["kind"] == "text"
        assert parts[0]["value"] == "Hello world"

    def test_single_jcode(self):
        text = '<j-code language="python">\n<j-line>print(42)</j-line>\n</j-code>'
        parts = extract_semantic_blocks(text)
        assert len(parts) == 1
        assert parts[0]["kind"] == "j-code"

    def test_text_around_jcode(self):
        text = "Before\n<j-code>\n<j-line>x=1</j-line>\n</j-code>\nAfter"
        parts = extract_semantic_blocks(text)
        assert len(parts) == 3
        assert parts[0]["kind"] == "text"
        assert parts[0]["value"] == "Before\n"
        assert parts[1]["kind"] == "j-code"
        assert parts[2]["kind"] == "text"
        assert parts[2]["value"] == "\nAfter"

    def test_multiple_blocks(self):
        text = '<j-code><j-line>a</j-line></j-code>\n<j-table><j-thead><j-th>X</j-th></j-thead></j-table>'
        parts = extract_semantic_blocks(text)
        assert len(parts) == 3  # j-code, text, j-table
        assert parts[0]["kind"] == "j-code"
        assert parts[1]["kind"] == "text"
        assert parts[2]["kind"] == "j-table"


class TestRenderSemanticMarkup:
    def test_no_semantic_tags_passthrough(self):
        assert render_semantic_markup("Plain text") == "Plain text"

    def test_empty_string(self):
        assert render_semantic_markup("") == ""

    def test_mixed_content(self):
        text = "Code:\n<j-code><j-line>hello</j-line></j-code>\nDone"
        result = render_semantic_markup(text)
        assert "Code:" in result
        assert "<pre><code>" in result
        assert "hello" in result
        assert "Done" in result

    def test_jtok_stripped(self):
        text = '<j-code><j-line><j-tok t="k">def</j-tok> f()</j-line></j-code>'
        result = render_semantic_markup(text)
        assert "<j-tok" not in result
        assert "def f()" in result
