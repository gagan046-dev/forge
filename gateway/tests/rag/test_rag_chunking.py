import pytest

from app.rag.indexer import CHUNK_LINES, CHUNK_OVERLAP, MAX_CHUNK_CHARS, chunk_file


def numbered(n: int) -> str:
    return "\n".join(f"line {i}" for i in range(1, n + 1))


def ranges(text: str) -> list[tuple[int, int]]:
    return [(c.start_line, c.end_line) for c in chunk_file("src/app.py", text)]


def test_defaults_match_spec():
    assert (CHUNK_LINES, CHUNK_OVERLAP) == (60, 10)


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (1, [(1, 1)]),
        (59, [(1, 59)]),
        (60, [(1, 60)]),
        (61, [(1, 60), (51, 61)]),
        (110, [(1, 60), (51, 110)]),
        (111, [(1, 60), (51, 110), (101, 111)]),
        (160, [(1, 60), (51, 110), (101, 160)]),
    ],
)
def test_chunk_boundaries(lines, expected):
    assert ranges(numbered(lines)) == expected


def test_empty_and_blank_files_produce_no_chunks():
    assert chunk_file("a.py", "") == []
    assert chunk_file("a.py", "\n\n   \n") == []


def test_overlap_repeats_last_ten_lines_and_header_names_the_file():
    first, second = chunk_file("pkg/mod.py", numbered(100))
    first_lines = first.content.splitlines()
    second_lines = second.content.splitlines()
    assert first_lines[0] == "# pkg/mod.py"
    assert second_lines[0] == "# pkg/mod.py"
    assert first_lines[1:] == [f"line {i}" for i in range(1, 61)]
    assert second_lines[1:] == [f"line {i}" for i in range(51, 101)]
    assert first_lines[-CHUNK_OVERLAP:] == second_lines[1 : 1 + CHUNK_OVERLAP]


def test_handles_windows_line_endings():
    chunks = chunk_file("a.txt", "one\r\ntwo\r\nthree\r\n")
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 3)]
    assert chunks[0].content == "# a.txt\none\ntwo\nthree"


def test_window_of_only_blank_lines_is_skipped():
    # lines 1-5 have text, 6-124 are blank, 125 is "tail": the 51-110 window holds nothing.
    text = numbered(5) + "\n" * 120 + "tail"
    assert ranges(text) == [(1, 60), (101, 125)]


def test_very_long_lines_are_capped():
    (chunk,) = chunk_file("data.json", "x" * 50_000)
    assert len(chunk.content) == MAX_CHUNK_CHARS
    assert (chunk.start_line, chunk.end_line) == (1, 1)


def test_custom_sizes_and_invalid_overlap():
    spans = [(c.start_line, c.end_line) for c in chunk_file("a", numbered(10), lines_per_chunk=4, overlap=1)]
    assert spans == [(1, 4), (4, 7), (7, 10)]
    with pytest.raises(ValueError):
        chunk_file("a", "x", lines_per_chunk=4, overlap=4)
