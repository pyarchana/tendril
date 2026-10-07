def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_creates_data_dirs(client, isolated_settings):
    assert isolated_settings.images_dir.is_dir()
    assert isolated_settings.audio_dir.is_dir()
