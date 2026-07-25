"""Public-route and dispatcher-script coverage."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth, store
from app.main import app
from app.models import UserProfile
from app.prompts import script_911


def test_every_public_page_and_crawler_surface_loads():
    with TestClient(app) as client:
        for path in (
            "/", "/app", "/caregiver", "/bystander", "/onboarding", "/ladder",
            "/home", "/emergency", "/contact", "/terms", "/privacy",
            "/data-deletion", "/login", "/register", "/register/caregiver",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"], path

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Disallow: /api/" in robots.text
        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "<urlset" in sitemap.text
        assert "/bystander" in sitemap.text


def test_contact_validation_and_persistence(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/contact", json={}).status_code == 400
        assert client.post("/api/contact", json={
            "name": "A", "email": "a@example.com", "message": "x" * 5001,
        }).status_code == 413
        response = client.post("/api/contact", json={
            "name": "A", "email": "a@example.com", "message": "Please contact me.",
        })
        assert response.json() == {"ok": True}
        assert "Please contact me." in (tmp_path / "contact_messages.jsonl").read_text()


def test_dispatcher_fact_validator_accepts_exact_and_rejects_changed():
    profile = UserProfile(
        id="member",
        username="member",
        name="Member",
        address="123 Exact Street",
        unit="4B",
        cross_street="Oak Avenue",
        entry_code="9081",
    )
    exact = script_911.render(profile)
    assert script_911.preserves_dispatcher_facts(exact, profile) is True
    assert script_911.preserves_dispatcher_facts(
        exact.replace("123 Exact Street", "123 Approximate Street"), profile
    ) is False
    assert script_911.preserves_dispatcher_facts("", profile) is False
