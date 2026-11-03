import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from soundbook.core.document import parse_book
from soundbook.core.webpage import html_to_book, normalize_url, parse_url

ARTICLE = """
<!doctype html>
<html>
  <head>
    <title>Site name - Quantum chips explained</title>
    <meta name="author" content="Jane Doe">
  </head>
  <body>
    <nav><a href="/">Home</a><a href="/world">World</a></nav>
    <header><h1>Example News</h1></header>
    <div class="newsletter"><p>Subscribe to our newsletter today.</p></div>
    <article>
      <h1>Quantum chips explained</h1>
      <p>Lead paragraph about quantum computing and why it matters.</p>
      <h2>Why it matters</h2>
      <p>Because certain computations become dramatically faster.</p>
      <h2>What comes next</h2>
      <p>More experiments and error correction are planned.</p>
    </article>
    <aside><p>Related stories you may like.</p></aside>
    <footer><p>Copyright 2026 Example News.</p></footer>
  </body>
</html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ARG002
        pass


@pytest.fixture
def article_url(tmp_path: Path):
    (tmp_path / "article.html").write_text(ARTICLE, encoding="utf-8")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(tmp_path))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/article.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_html_to_book_extracts_article():
    book = html_to_book(ARTICLE)

    text = "\n\n".join(chapter.text for chapter in book.chapters)
    assert book.title == "Quantum chips explained"
    assert book.author == "Jane Doe"
    assert "Lead paragraph about quantum computing" in text
    assert "Home" not in text
    assert "newsletter" not in text
    assert "Related stories" not in text
    assert "Copyright" not in text
    assert [chapter.title for chapter in book.chapters] == [
        "Quantum chips explained",
        "Why it matters",
        "What comes next",
    ]


def test_parse_html_file(tmp_path: Path):
    path = tmp_path / "article.html"
    path.write_text(ARTICLE, encoding="utf-8")

    book = parse_book(path)
    assert book.title == "Quantum chips explained"
    assert len(book.chapters) == 3


def test_parse_url_rejects_non_http_links():
    with pytest.raises(ValueError):
        parse_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        parse_url("ftp://example.com/article")


def test_normalize_url_adds_https():
    assert normalize_url("example.com/story") == "https://example.com/story"
    assert normalize_url(" https://example.com/story ") == "https://example.com/story"
    with pytest.raises(ValueError):
        normalize_url("")


def test_parse_url_fetches_and_extracts(article_url: str):
    book, source = parse_url(article_url)

    assert source == article_url
    assert book.title == "Quantum chips explained"
    assert "dramatically faster" in "\n".join(
        chapter.text for chapter in book.chapters
    )