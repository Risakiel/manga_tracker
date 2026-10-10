from sqlalchemy import create_engine, inspect, text

from app import database


def test_missing_komga_columns_are_added_to_an_existing_database(monkeypatch, tmp_path):
    # An existing install's DB predates the Komga box columns -- startup
    # must add them in place without touching the rows already there.
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                'CREATE TABLE manga (id INTEGER PRIMARY KEY, category VARCHAR, title_en VARCHAR, '
                "server_folder VARCHAR, komga_series_id VARCHAR)"
            )
        )
        conn.execute(text("INSERT INTO manga (category, title_en, server_folder, komga_series_id) VALUES ('manga', 'Old', 'Old', 'ABC123')"))
    monkeypatch.setattr(database, "engine", engine)

    database.init_db()

    columns = {col["name"] for col in inspect(engine).get_columns("manga")}
    assert {"komga_series_name", "komga_folder_path", "komga_mangaupdates_url", "komga_id_pinned", "komga_synced_at"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT title_en, komga_series_id FROM manga")).one() == ("Old", "ABC123")
