import pytest

from him_workbench.assistant import _host


def test_bare_host_gets_http():
    assert _host("localhost:11434") == "http://localhost:11434"


def test_http_and_https_are_kept_and_trailing_slash_dropped():
    assert _host("http://10.0.0.5:11434/") == "http://10.0.0.5:11434"
    assert _host("https://ollama.example.com") == "https://ollama.example.com"


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://example.com", "gopher://example.com"])
def test_other_schemes_are_refused(bad):
    with pytest.raises(ValueError, match="http"):
        _host(bad)
