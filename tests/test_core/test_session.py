from pathlib import Path

from re_agent.core.session import Session


def test_session_save_replaces_existing_file(tmp_path: Path) -> None:
    session_file = tmp_path / "progress.json"

    session = Session(session_file)
    session.save()

    assert session_file.exists()

    session._data["runs"].append({"test": 1})
    session.save()

    assert session_file.exists()