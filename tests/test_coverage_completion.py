"""Coverage closure for HTTP boundary and defensive integration paths."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import auth, deps, email, genai, registration, security, store
from app.main import app
import app.main as main
from app.models import Contact, Event, Generation, LadderConfig, Tier, UserProfile, VaultClip


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_db_path", tmp_path / "coverage.db")
    monkeypatch.setattr(genai, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("THRESHOLD_SECRET", "coverage-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    deps._tiers.clear()
    deps._listeners.clear()
    security._hits.clear()
    store.init_db()
    yield tmp_path
    deps._tiers.clear()
    deps._listeners.clear()
    security._hits.clear()


def _account(name: str, *, role: str = "user", email_address: str | None = None):
    password_hash, salt = auth.hash_password("threshold")
    return store.create_user(
        id=uuid.uuid4().hex,
        username=name,
        password_hash=password_hash,
        salt=salt,
        role=role,
        email=email_address or f"{name}@example.com",
        email_verified=True,
        full_name=name.title(),
        phone="+919999999999",
    )


def _login(client: TestClient, account) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": account.email or account.username, "password": "threshold"},
    )
    assert response.status_code == 200


def _profile(account, **overrides) -> UserProfile:
    values = {
        "id": f"profile-{account.id}",
        "name": account.full_name or account.username,
        "address": "1 Test Road",
        "state_code": "KA",
    }
    values.update(overrides)
    profile = UserProfile(**values)
    store.put_profile(account.id, profile)
    return profile


@pytest.mark.asyncio
async def test_emergency_delivery_covers_linked_and_contact_recipients(
    isolated, monkeypatch
):
    member = _account("member")
    caregiver = _account("care", role="caregiver")
    store.link_caregiver(caregiver.id, member.id)
    _profile(
        member,
        contacts=[
            Contact(
                name="Duplicate",
                relation="friend",
                channel="email",
                destination=caregiver.email,
                order=1,
                tiers=[Tier.EMERGENCY],
            ),
            Contact(
                name="Second",
                relation="friend",
                channel="email",
                destination="second@example.com",
                order=2,
                tiers=[Tier.EMERGENCY],
            ),
            Contact(
                name="Phone",
                relation="friend",
                channel="phone",
                destination="123",
                order=3,
                tiers=[Tier.EMERGENCY],
            ),
        ],
    )
    sent: list[str] = []

    async def send(destination, *_args, **_kwargs):
        sent.append(destination)
        return True, None

    monkeypatch.setattr(main.email_delivery, "send_caregiver_alert", send)
    stored_profile = store.get_profile(member.id)
    stored_profile.contacts = [
        Contact(
            name="Duplicate",
            relation="friend",
            channel="email",
            destination=caregiver.email,
            order=1,
            tiers=[Tier.EMERGENCY],
        ),
        Contact(
            name="Second",
            relation="friend",
            channel="email",
            destination="second@example.com",
            order=2,
            tiers=[Tier.EMERGENCY],
        ),
        Contact(
            name="Phone",
            relation="friend",
            channel="phone",
            destination="123",
            order=3,
            tiers=[Tier.EMERGENCY],
        ),
    ]
    monkeypatch.setattr(store, "get_profile", lambda _user_id: stored_profile)
    event = Event(
        id="emergency",
        user_id=member.id,
        at=datetime.now(),
        tier=Tier.EMERGENCY,
        trigger_source="test",
        reason="test",
    )
    await main._deliver_emergency_alerts(member.id, event)
    assert sent == [caregiver.email, "second@example.com"]
    assert len(store.list_events(member.id)) == 2
    await main._deliver_emergency_alerts(
        member.id, event.model_copy(update={"tier": Tier.BASELINE})
    )


@pytest.mark.asyncio
async def test_emergency_delivery_skips_unverified_and_failed_delivery(
    isolated, monkeypatch
):
    member = _account("member")
    caregiver = auth.register(
        "care", "threshold", role="caregiver"
    )
    store.link_caregiver(caregiver.id, member.id)
    _profile(
        member,
        contacts=[
                Contact(
                    name="No tier",
                    relation="friend",
                    channel="email",
                    destination="other@example.com",
                    order=1,
                    tiers=[Tier.UNRESPONSIVE],
                ),
                Contact(
                    name="Blank",
                    relation="friend",
                    channel="email",
                    destination="",
                    order=2,
                    tiers=[Tier.EMERGENCY],
                ),
        ],
    )

    async def fail(*_args, **_kwargs):
        return False, "no"

    monkeypatch.setattr(main.email_delivery, "send_caregiver_alert", fail)
    event = Event(
        id="e",
        user_id=member.id,
        at=datetime.now(),
        tier=Tier.EMERGENCY,
        trigger_source="test",
        reason="test",
    )
    await main._deliver_emergency_alerts(member.id, event)
    assert store.list_events(member.id) == []


def test_auth_registration_and_me_endpoint_branches(isolated, monkeypatch):
    pending = SimpleNamespace(email="new@example.com")

    async def begin_ok(**_kwargs):
        return pending

    async def begin_bad(**_kwargs):
        raise registration.RegistrationError("bad signup")

    with TestClient(app) as client:
        assert client.get("/api/auth/me").json() == {"signed_in": False}
        monkeypatch.setattr(registration, "begin", begin_bad)
        assert client.post(
            "/api/auth/register",
            json={
                "email": "new@example.com",
                "full_name": "New User",
                "phone": "+919999999999",
                "password": "password123",
                "role": "user",
            },
        ).status_code == 400
        monkeypatch.setattr(registration, "begin", begin_ok)
        assert client.post(
            "/api/auth/register",
            json={
                "email": "new@example.com",
                "full_name": "New User",
                "phone": "+919999999999",
                "password": "password123",
                "role": "user",
            },
        ).status_code == 202

        member = _account("member")
        _login(client, member)
        assert client.get("/api/auth/me").json()["signed_in"] is True


def test_registration_verification_error_and_caregiver_warning(isolated, monkeypatch):
    caregiver = _account("caregiver", role="caregiver")

    def bad(*_args, **_kwargs):
        raise registration.RegistrationError("bad code")

    with TestClient(app) as client:
        monkeypatch.setattr(registration, "complete", bad)
        assert client.post(
            "/api/auth/register/verify",
            json={"email": "x@example.com", "code": "123456"},
        ).status_code == 400

        monkeypatch.setattr(
            registration,
            "complete",
            lambda *_args, **_kwargs: registration.RegistrationResult(
                caregiver, watching=None, link_error="expired"
            ),
        )
        body = client.post(
            "/api/auth/register/verify",
            json={"email": "x@example.com", "code": "123456"},
        ).json()
        assert body["linked"] is False
        assert body["link_error"] == "expired"


def test_main_api_fallbacks_and_profile_validation(isolated, monkeypatch):
    member = _account("member")
    with TestClient(app) as client:
        _login(client, member)
        original_import = __import__

        def broken_import(name, *args, **kwargs):
            if name == "app.genai":
                raise ImportError("offline")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", broken_import)
        assert client.get("/api/state").status_code == 200
        response = client.post("/api/utterance", json={"text": "hello"})
        assert response.status_code == 200
        assert response.json()["reply"]["live"] is False
        monkeypatch.setattr("builtins.__import__", original_import)

        assert client.get("/api/script/112").status_code == 409
        created = client.post(
            "/api/profile",
            json={
                "address": "2 New Street",
                "unit": "4A",
                "entry_code": "9911",
                "cross_street": "Main",
                "state_code": "MH",
                "naloxone_on_hand": True,
                    "ladder": {
                        "tier_2_visible_to_caregiver": True,
                        "tier_3_visible_to_caregiver": True,
                        "silence_seconds_to_escalate": 300,
                        "missed_checkins_to_elevate": 1,
                },
                "contacts": [
                    {
                        "name": "Helper",
                        "relation": "Friend",
                        "channel": "email",
                        "destination": "helper@example.com",
                        "tiers": [4, 5],
                    }
                ],
            },
        )
        assert created.status_code == 200
        profile = created.json()["profile"]
        assert profile["ladder"]["silence_seconds_to_escalate"] == 300
        assert profile["ladder"]["missed_checkins_to_elevate"] == 1

        for contacts in (
            "wrong",
            [{}],
            ["wrong"],
            [{"name": "x", "channel": "email", "destination": "invalid"}],
            [{"name": "x", "tiers": ["bad"]}],
            [{"name": str(i)} for i in range(11)],
        ):
            assert client.post("/api/profile", json={"contacts": contacts}).status_code in (
                400,
                422,
            )


def test_main_remaining_helpers_and_page_branches(isolated, monkeypatch, tmp_path):
    from app import legal

    with pytest.raises(HTTPException):
        main._parse_contacts("not-a-list")
    with pytest.raises(HTTPException):
        main._parse_contacts([{"name": str(i)} for i in range(11)])
    with pytest.raises(HTTPException):
        main._parse_contacts(["not-an-object"])
    assert main._parse_contacts([{"name": "A", "channel": ""}])[0].channel == "phone"
    with pytest.raises(HTTPException):
        main._parse_contacts([{"name": "A", "tiers": ["unknown"]}])

    monkeypatch.setattr(genai, "is_online", lambda: (_ for _ in ()).throw(RuntimeError()))
    member = _account("member")
    _profile(member)
    with TestClient(app) as client:
        _login(client, member)
        assert client.get("/api/state").json()["ai_online"] is False
        assert client.get("/").status_code == 200
        assert client.get("/legacy-app").status_code == 200
        legal.load()
        assert client.get("/api/legal/KA").status_code == 200


def test_action_receipts_enforce_role_and_record(isolated):
    member = _account("member")
    caregiver = _account("caregiver", role="caregiver")
    with TestClient(app) as client:
        assert client.post(
            "/api/action-receipt", json={"action": "location_displayed"}
        ).status_code == 401
        _login(client, caregiver)
        assert client.post(
            "/api/action-receipt", json={"action": "location_displayed"}
        ).status_code == 403
        client.post("/api/auth/logout")
        _login(client, member)
        response = client.post(
            "/api/action-receipt",
            json={"action": "location_displayed", "detail": "Displayed location"},
        )
        assert response.status_code == 200
        assert response.json()["event"]["actions_taken"] == ["location_displayed"]


def test_generation_endpoints_success_and_fallbacks(isolated, monkeypatch):
    member = _account("member")
    profile = _profile(member, entry_code="1234")

    async def live(*_args, **_kwargs):
        return {
            "text": f"Address {profile.address}; entry code {profile.entry_code}.",
            "live": True,
            "model": "test",
            "latency_ms": 1,
            "error": None,
        }

    with TestClient(app) as client:
        _login(client, member)
        monkeypatch.setattr(main, "_generate", live)
        assert client.get("/api/script/112").json()["deterministic"] is False
        assert client.get("/api/script/refusal").status_code == 200
        assert "window_active" in client.get("/api/tolerance").json()
        assert client.get("/api/vault/select").json()["clip"] is None

        clip = VaultClip(
            id="clip",
            recorded_by="Care",
            relation="friend",
            transcript="Stay with me.",
            tags=["steady"],
            owner_user_id=member.id,
        )
        store.put_vault_clip(clip)

        async def vault_error(*_args, **_kwargs):
            raise RuntimeError("selector failed")

        monkeypatch.setattr(genai, "vault_select", vault_error)
        body = client.get("/api/vault/select").json()
        assert body["clip"]["id"] == "clip"
        assert body["live"] is False

        async def vault_ok(*_args, **_kwargs):
            return clip, Generation(
                text="best fit", live=True, model="test", latency_ms=1
            )

        monkeypatch.setattr(genai, "vault_select", vault_ok)
        assert client.get("/api/vault/select").json()["why"] == "best fit"


def test_caregiver_state_checkins_and_brief_visibility(isolated, monkeypatch):
    member = _account("member")
    caregiver = _account("caregiver", role="caregiver")
    _profile(
        member,
        ladder=LadderConfig(tier_3_visible_to_caregiver=True),
    )
    store.link_caregiver(caregiver.id, member.id)
    store.append_event(
        Event(
            id="utterance",
            user_id=member.id,
            at=datetime.now(),
            tier=Tier.ACTIVE_USE,
            trigger_source="utterance",
            reason="shared summary",
        )
    )

    async def generated(*_args, **_kwargs):
        return {"text": "brief", "live": True}

    with TestClient(app) as client:
        _login(client, caregiver)
        deps._tiers[member.id] = Tier.BASELINE
        assert client.get("/api/caregiver/brief").status_code == 403
        deps._tiers[member.id] = Tier.ACTIVE_USE
        state = client.get("/api/state").json()
        assert state["checkins"][0]["shared"] is True
        assert state["checkins"][0]["summary"] == "shared summary"
        monkeypatch.setattr(main, "_generate", generated)
        assert client.get("/api/caregiver/brief").json()["tier"] == 3


def test_known_legal_missing_page_and_account_deletion(isolated, monkeypatch, tmp_path):
    member = _account("member")
    _profile(member)
    monkeypatch.setattr(main, "WEB_DIR", tmp_path)
    assert main._page("missing.html").status_code == 503

    removed: list[str] = []
    monkeypatch.setattr(genai, "cache_delete", lambda key: removed.append(key) or True)
    store.record_cache_owner("cache-key", member.id)
    listener = deps.Listener(member.id, asyncio.Queue(maxsize=1))
    deps._listeners.append(listener)
    deps._tiers[member.id] = Tier.CRAVING

    async def no_voices(_user_id):
        return 0

    from app.routes import voice as voice_routes

    monkeypatch.setattr(voice_routes, "purge_voices_for_user", no_voices)
    with TestClient(app) as client:
        assert client.post("/api/account/delete").status_code == 401
        _login(client, member)
        response = client.post("/api/account/delete")
        assert response.status_code == 200
        assert removed == ["cache-key"]
        assert listener not in deps._listeners


@pytest.mark.asyncio
async def test_sse_generator_emits_payload_timeout_and_disconnect(isolated, monkeypatch):
    states = iter([False, False])

    async def disconnected():
        return next(states)

    request = SimpleNamespace(is_disconnected=disconnected)
    response = await main.sse(request)
    iterator = response.body_iterator
    assert "event: ping" in await iterator.__anext__()
    listener = deps._listeners[-1]
    listener.queue.put_nowait({"tier": 1})
    assert '"tier": 1' in await iterator.__anext__()

    real_wait_for = asyncio.wait_for

    async def timeout(*_args, **_kwargs):
        _args[0].close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    assert "event: ping" in await iterator.__anext__()
    monkeypatch.setattr(asyncio, "wait_for", real_wait_for)
    async def yes():
        return True
    request.is_disconnected = yes
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()
    assert listener not in deps._listeners
