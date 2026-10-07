from mercury.paths import migrate_legacy_files


def test_migrates_legacy_harvey_files(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "harvey.db").write_text("db")
    (tmp_path / "harvey.local.yaml").write_text("cfg")

    moved = migrate_legacy_files(tmp_path)

    assert (tmp_path / "data" / "mercury.db").read_text() == "db"
    assert (tmp_path / "mercury.local.yaml").read_text() == "cfg"
    assert not (tmp_path / "data" / "harvey.db").exists()
    assert len(moved) == 2


def test_never_overwrites_existing_mercury_files(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "harvey.db").write_text("old")
    (tmp_path / "data" / "mercury.db").write_text("new")

    assert migrate_legacy_files(tmp_path) == []
    assert (tmp_path / "data" / "mercury.db").read_text() == "new"
    assert migrate_legacy_files(tmp_path) == []
