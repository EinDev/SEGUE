"""Smoke test für den Anwendungsauf- und -abbau.

Die übrigen Tests importieren nur einzelne Module (app.db, app.state,
app.mediamtx_stats) -- main.py und damit die gesamte Start-/Abbau-Phase war
bisher ungetestet. Genau dort schlagen Framework-Upgrades aber zu: der
Wechsel von @app.on_event auf einen lifespan-Handler wäre lautlos
durchgegangen, weil ein nicht mehr ausgeführter Startup-Handler keinen
Importfehler erzeugt -- die Anwendung startet einfach mit leerem
app.state und fällt erst beim ersten echten Request um.

Dieser Test geht deshalb über den TestClient-Kontextmanager, der die
lifespan-Phase tatsächlich durchläuft, und prüft hinterher, dass der
Startup-Code wirklich lief.
"""
import os
import secrets

import pytest
from fastapi.testclient import TestClient

# Pflichtvariablen, ohne die die Startphase abbricht. Die Geheimnis-artigen
# Werte werden je Lauf erzeugt statt im Klartext hinterlegt: feste
# Zeichenketten wie "test-secret" sind zwar wertlos, loesen aber
# Geheimnis-Scanner aus und gewoehnen einem an, so etwas einzuchecken.
REQUIRED_ENV = {
    "ONAIR_ADMIN_USERNAME": "admin",
    "ONAIR_INTERNAL_SECRET": secrets.token_hex(16),
    "ONAIR_LJ_TOKEN": secrets.token_hex(16),
    "ONAIR_LJ_READ_USERNAME": "reader",
    "ONAIR_LJ_READ_PASSWORD": secrets.token_hex(16),
    "ONAIR_RTMP_PUBLIC_HOST": "rtmp.example.invalid",
    "ONAIR_RTSP_PUBLIC_HOST": "rtsp.example.invalid",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ONAIR_DB_PATH", str(tmp_path / "onair.db"))
    import app.main as main

    with TestClient(main.app) as c:
        yield c, main


def test_lifespan_runs_startup(client):
    """Die Startup-Phase muss app.state tatsächlich befüllen."""
    _, main = client
    assert main.app.state.admin_username == REQUIRED_ENV["ONAIR_ADMIN_USERNAME"]
    assert isinstance(main.app.state.max_djs, int)
    assert hasattr(main.app.state, "slot_occupants")


def test_healthz(client):
    c, _ = client
    response = c.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_no_deprecated_event_hooks():
    """@app.on_event ist veraltet; der Wechsel darauf zurück soll auffallen."""
    import app.main as main

    path = os.path.join(os.path.dirname(main.__file__), "main.py")
    with open(path, encoding="utf-8") as handle:
        # Nur echte Dekoratorzeilen, nicht jede Erwähnung: der lifespan-Handler
        # nennt @app.on_event in seinem Docstring, und daran soll der Test sich
        # nicht stören.
        decorators = [
            line for line in handle if line.lstrip().startswith("@app.on_event")
        ]
    assert not decorators, f"veraltete Event-Hooks gefunden: {decorators}"
