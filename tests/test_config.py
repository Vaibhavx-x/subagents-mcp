"""Server configuration: reading .env without silently dropping it.

Everything here exists because a setting that fails to apply looks exactly like
a setting that applied. There is no error to notice -- only a worker that runs
for the wrong length of time, long after you stopped watching.
"""

from __future__ import annotations
# ------------------------------------------------- .env that silently does nothing
def test_a_utf8_bom_does_not_eat_the_first_key(tmp_path):
    """PowerShell 5.1's `Out-File -Encoding utf8` writes a BOM.

    Read as plain utf-8 the first key becomes '\ufeffSUBAGENTS_...', matches
    nothing, and the default applies with no error. Measured the hard way: a
    worker given a 45s budget ran for 106s and reported success, because the
    budget was never read. NOTES.md section 23.
    """
    from subagents.config import _load_dotenv, load_config

    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfSUBAGENTS_WORKER_TIMEOUT_S=45\r\n")

    assert _load_dotenv(env) == {"SUBAGENTS_WORKER_TIMEOUT_S": "45"}
    assert load_config(env).worker_timeout_s == 45


def test_crlf_and_quotes_survive_too(tmp_path):
    from subagents.config import _load_dotenv

    env = tmp_path / ".env"
    env.write_bytes(b'SUBAGENTS_MODEL="gemini-x"\r\nSUBAGENTS_MAX_PARALLEL=2\r\n')
    assert _load_dotenv(env) == {"SUBAGENTS_MODEL": "gemini-x",
                                 "SUBAGENTS_MAX_PARALLEL": "2"}


def test_a_misspelled_key_is_reported_not_ignored(tmp_path):
    """The README criticises the client for accepting typos silently. Doing the
    same thing ourselves would be worse."""
    from subagents.config import _load_dotenv, unknown_keys

    env = tmp_path / ".env"
    env.write_text("SUBAGENTS_WORKER_TIMEOUTS=45\nSUBAGENTS_MODEL=m\n", encoding="utf-8")
    assert unknown_keys(_load_dotenv(env)) == ["SUBAGENTS_WORKER_TIMEOUTS"]


def test_every_documented_key_is_known(tmp_path):
    """.env.example and KNOWN_KEYS must not drift apart."""
    import re

    from conftest import REPO_ROOT
    from subagents.config import KNOWN_KEYS

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z_]+)=", text, flags=re.M))
    assert documented <= KNOWN_KEYS, f"undocumented in KNOWN_KEYS: {documented - KNOWN_KEYS}"
