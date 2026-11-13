import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    """Cada test con su propia carpeta de datos y su propia base SQLite."""
    monkeypatch.setenv("HOOKLINE_DATOS", str(tmp_path / "datos"))
    monkeypatch.setenv("HOOKLINE_CLAVE_SECRETA", Fernet.generate_key().decode())
    monkeypatch.setenv("HOOKLINE_CLAVE_SESION", "clave-de-sesion-para-tests-123456")
    monkeypatch.setenv("HOOKLINE_URL_PUBLICA", "https://hookline.test")
    monkeypatch.setenv("HOOKLINE_MOTOR_IA", "claude_code")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HOOKLINE_WEBHOOK_URL", raising=False)
    from hookline import config, db
    config.recargar()
    db.reiniciar_motor()
    db.crear_tablas()
    from hookline import fuentes
    fuentes._indice.cache_clear()
    yield tmp_path
    db.reiniciar_motor()


@pytest.fixture
def cuenta_conectada():
    """Una cuenta con token válido y todos los permisos."""
    from hookline import instagram as ig
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id="modelo1", ig_user_id="1789", usuario_ig="modelo1", nicho="humor", tono="pícaro"))
    t = time.time()
    ig.guardar_credencial("modelo1", {"access_token": "TOKEN-SECRETO", "permisos": ig.SCOPES,
                                      "obtenido": t, "refrescado": t, "vence": t + 50 * 86400})
    return "modelo1"


@pytest.fixture
def cliente_api():
    from fastapi.testclient import TestClient
    from hookline import api
    from hookline.auth import crear_usuario
    crear_usuario("duena@hookline.test", "una-clave-larga", "Dueña", "admin")
    c = TestClient(api.app)
    r = c.post("/api/login", json={"email": "duena@hookline.test", "clave": "una-clave-larga"})
    assert r.status_code == 200
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


@pytest.fixture
def foto(tmp_path):
    from PIL import Image
    p = tmp_path / "foto.jpg"
    Image.new("RGB", (1080, 1350), (200, 120, 150)).save(p)
    return p


class MetaFalsa:
    """Reemplaza instagram.pedir: guarda las llamadas y contesta como Meta."""

    def __init__(self):
        self.llamadas = []
        self.status = {}

    def __call__(self, metodo, url, params=None, datos=None, timeout=60, headers=None):
        self.llamadas.append((metodo, url, dict(params or {}), dict(datos or {})))
        if url.endswith("/content_publishing_limit"):
            return {"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]}
        if url.endswith("/media_publish"):
            return {"id": "MEDIA-1"}
        if url.endswith("/media"):
            return {"id": "CONT-1"}
        if "CONT-1" in url:
            return {"status_code": "FINISHED"}
        if "MEDIA-1" in url:
            return {"permalink": "https://www.instagram.com/p/xyz/", "timestamp": "2026-11-20T23:00:00+0000"}
        if url.endswith("/refresh_access_token"):
            return {"access_token": "TOKEN-NUEVO", "expires_in": 5184000}
        return {}


@pytest.fixture
def meta(monkeypatch):
    m = MetaFalsa()
    from hookline import instagram
    monkeypatch.setattr(instagram, "pedir", m)
    return m
