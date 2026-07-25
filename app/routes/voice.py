"""The consented supporter-voice HTTP surface.

WHAT THIS MODULE DOES
    Implements the six endpoints behind the supporter-voice feature, and — more
    importantly than any of them — implements THE CONSENT GATE. Read
    `app/voice.py`'s module docstring before changing anything here; it is the
    specification this file exists to enforce, and it is candid that the
    objections in PRD §7.2 were not answered, only mitigated.

THE CONSENT CHAIN, AND WHERE EACH LINK IS ENFORCED
    1. A caregiver, in their own signed-in account, opts in.
       -> `_require_caregiver()` on POST /api/voice/clone.
    2. They record themselves reading the sample script, in the browser.
       -> the audio arrives as multipart on their own authenticated request;
          `GET /api/voice/script` serves the passages.
    3. They tick an explicit, specific consent statement before anything
       uploads. -> `consent` must be literally true, and the exact wording is
          stored verbatim on the row (`voice_store.CONSENT_TEXT`).
    4. We build the clone. -> `voice.clone_supporter_voice()`.
    5. The caregiver SEPARATELY chooses to share it with their linked member.
       -> POST /api/voice/share; rows are created `shared=0` and this module has
          no path that creates a shared row.
    6. The member SEPARATELY chooses to use it, default off.
       -> the server never selects a voice; GET /api/voice/available merely
          offers, and the member's choice lives in their own browser.
    7. Either side may revoke at any time.
       -> DELETE /api/voice/{id}, permitted to the owning caregiver OR the
          member it is shared with, and it deletes the model upstream first.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * It never accepts a `caregiver_user_id` from a client. The account being
      cloned is always the authenticated caller. An id in a request body is a
      request, not a permission — the same rule the caregiver privacy boundary
      is built on (`app/store.py`).
    * It never synthesizes a Memory Vault clip. Vault clips are REAL recordings
      of a real person and play as recorded (PRD §7.2; `app/voice.py`). Nothing
      in this file reads `vault_clips`, so that is structural rather than
      careful.
    * It never lets a cloned voice claim presence. `_refuse_presence_claim()`
      rejects text implying the real person is live, listening, or on the line
      (PRD P5) — the one line this feature does not cross.
    * It makes no triage decision and holds no clinical logic.
    * It never puts the provider API key on the wire. Synthesis is server-side
      for exactly that reason; the client receives audio bytes and never a
      credential, and `app/voice.py` redacts the key from anything it logs.

DEFAULT OFF AT EVERY STEP
    No cloning without an explicit tick. No sharing without a separate explicit
    action. No use without the member choosing it. Nothing here has a default
    that results in a synthetic voice speaking.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app import store, voice, voice_store

router = APIRouter()

# Ceilings on an authenticated multipart upload. Generous enough for the
# 30-60 seconds of speech the script asks for at browser-recorder bitrates, and
# small enough that this endpoint cannot be used to fill the disk. Bounded here
# rather than trusted from the client, which can claim any length it likes.
_MAX_SAMPLES = 6
_MAX_SAMPLE_BYTES = 12 * 1024 * 1024        # per passage
_MAX_TOTAL_BYTES = 30 * 1024 * 1024         # across the whole submission

# The upper bound on one spoken line. Matches the ceiling in `app/voice.py`, and
# is enforced again here because this endpoint costs money per call: an
# unbounded body would be a billing attack as much as a resource one.
_MAX_SPEAK_CHARS = 900

# Phrases a cloned supporter voice must never say. PRD P5: the system never
# claims to be a person, and a copy of someone's mother saying "I'm here with
# you" to a person mid-overdose is the precise harm §7.2 objected to — consent
# is obtained while calm and spent during crisis, and someone in that state
# cannot process an on-screen label contradicting a voice they trust.
#
# This is a blunt substring check and it is honestly a weak control: it catches
# the phrasings our own product would plausibly generate, not an adversary's,
# and it cannot catch a paraphrase. It exists because the alternative — no check
# at all — means the first presence claim ships silently. The real protection is
# that every caller passes text the app itself composed.
_PRESENCE_CLAIMS = (
    "i'm here with you",
    "i am here with you",
    "i'm here",
    "i am here",
    "i'm with you",
    "i am with you",
    "i'm on my way",
    "i am on my way",
    "i'm listening",
    "i am listening",
    "i can hear you",
    "i'm right here",
    "i am right here",
    "it's really me",
    "it is really me",
    "i'm on the line",
    "i am on the line",
    "stay on the line with me",
)


def _now() -> datetime:
    """Single source of 'now' for this module, in UTC."""
    return datetime.now(timezone.utc)


def _require_user(request: Request):
    """Resolve the caller, or refuse.

    STRICTER THAN `deps._session_user`, which falls back to the published demo
    account so an evaluator poking at an endpoint sees the product work. That
    trade is right for read-only surfaces and wrong here: an anonymous caller
    silently becoming "sam" on a voice endpoint would mean a stranger could
    attribute a voice model to a real account, or hear a supporter's cloned
    voice they were never shared. There is no demo fallback anywhere in this
    file, deliberately.

    Args:
        request: The incoming request.

    Returns:
        The authenticated `store.UserRecord`.

    Raises:
        HTTPException: 401 when there is no valid session.
    """
    from app import auth

    user = auth.user_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to use voice features.")
    return user


def _require_caregiver(request: Request):
    """Resolve the caller and assert they hold the caregiver role.

    Link 1 of the consent chain. A member cannot clone a voice at all — not
    their own and certainly not anyone else's — because this feature exists
    solely so a supporter can offer their own voice, and any other shape of it
    is the abuse case.

    Args:
        request: The incoming request.

    Returns:
        The authenticated caregiver `store.UserRecord`.

    Raises:
        HTTPException: 401 with no session; 403 for a signed-in non-caregiver.
            403 and not 401 — the caller IS authenticated, and a 401 would
            bounce a signed-in user into a login loop.
    """
    user = _require_user(request)
    if user.role != "caregiver":
        raise HTTPException(
            status_code=403,
            detail="Only a supporter's own account can record a supporter voice.",
        )
    return user


def _refuse_presence_claim(text: str) -> None:
    """Reject text that would make a cloned voice claim presence.

    PRD P5, and the line named in `app/voice.py` as the one this feature does
    not cross. Applied ONLY to cloned voices: a stock narrator saying "I'm here"
    is a UI copy problem, while a copy of a specific person's voice saying it is
    an impersonation that lands on a real relationship.

    Args:
        text: What is about to be spoken.

    Raises:
        HTTPException: 400 if the text implies the real person is live. Failing
            the request outright rather than quietly rewriting it: a silent edit
            would hide a caller that keeps generating presence claims, and the
            client falls back to browser speech, so nobody is left in silence.
    """
    lowered = text.lower()
    if any(claim in lowered for claim in _PRESENCE_CLAIMS):
        raise HTTPException(
            status_code=400,
            detail=(
                "A supporter's cloned voice may never say the real person is "
                "present or listening."
            ),
        )


def _public_voice(row: voice_store.SupporterVoice) -> dict:
    """Project a row for the MEMBER's picker.

    Deliberately narrow. The member gets a label, our row id, and the provider
    id needed to request speech. They do NOT get `consent_text` or
    `caregiver_user_id`: the consent statement is the supporter's record of what
    the supporter agreed to, and it is not the member's to read.

    `cloned: true` travels with every entry so the UI can label it without
    inferring anything at render time — the same explicit-carry rule
    `voice.Speech.cloned` follows, and what makes the "AI recreation" label
    impossible to forget to show.

    Args:
        row: The stored voice.

    Returns:
        A JSON-safe dict.
    """
    return {
        "id": row.id,
        "voice_id": row.voice_id,
        "name": row.display_name,
        "cloned": True,
        "source": "supporter",
    }


def _owner_voice(row: voice_store.SupporterVoice) -> dict:
    """Project a row for the OWNING CAREGIVER's own page.

    Includes `consent_text` and `consented_at`, because the person who agreed is
    entitled to see exactly what they agreed to and when — shown back to them on
    the page rather than filed somewhere they would have to ask for. That is the
    difference between recording consent and honouring it.

    Args:
        row: The stored voice.

    Returns:
        A JSON-safe dict.
    """
    return {
        "id": row.id,
        "voice_id": row.voice_id,
        "name": row.display_name,
        "shared": row.shared,
        "consent_text": row.consent_text,
        "consented_at": row.consented_at.isoformat(),
        "created_at": row.created_at.isoformat(),
        "cloned": True,
    }


# ---------------------------------------------------------------------------
# Status and script
# ---------------------------------------------------------------------------
@router.get("/api/voice/status")
async def voice_status(request: Request) -> dict:
    """Whether cloud voice is configured, reported honestly.

    Returns `online: false` rather than an error when no key is set. The app
    must boot and work without one (CONTRACT.md), and the UI renders an explicit
    offline state instead of letting a missing key look like a broken feature.

    Never returns the key or any part of it — that is the whole reason synthesis
    is server-side.
    """
    online = voice.is_online()
    payload: dict = {"online": online}

    # Tell a signed-in caregiver whether they already have a voice, so their page
    # can open in the right state rather than flashing the recorder at someone
    # who finished this weeks ago.
    from app import auth

    user = auth.user_from_request(request)
    if user and user.role == "caregiver":
        payload["voices"] = [
            _owner_voice(v) for v in voice_store.list_for_caregiver(user.id)
        ]
    return payload


@router.get("/api/voice/script")
async def voice_script() -> dict:
    """The passages a supporter is asked to read, plus the consent wording.

    Served from the server rather than hardcoded in the page so that the text
    shown to the supporter and the text stored on their row come from one
    source. If those two ever drifted, the stored consent would document
    something the person never actually saw.
    """
    return {
        "passages": list(voice_store.SAMPLE_SCRIPT),
        "consent_text": voice_store.CONSENT_TEXT,
    }


# ---------------------------------------------------------------------------
# Cloning — THE GATE
# ---------------------------------------------------------------------------
@router.post("/api/voice/clone")
async def voice_clone(
    request: Request,
    consent: str = Form(...),
    display_name: str = Form(""),
    samples: list[UploadFile] = File(...),
) -> dict:
    """Build a voice model from the authenticated caregiver's own recordings.

    THIS IS THE GATE. `voice.clone_supporter_voice()` states plainly that it
    cannot verify who is speaking in the audio and that establishing it is the
    caller's job. Everything that makes this feature defensible is enforced in
    this function:

      * ROLE. Caregivers only (`_require_caregiver`).
      * IDENTITY. The owner is the session's account. There is no
        `caregiver_user_id` parameter, so no request can name a different one.
        The audio arrived on this person's own authenticated request, recorded
        in this person's own browser.
      * CONSENT. `consent` must be exactly "true". Absent, empty, "false", or
        anything else is a 400 — and the check happens BEFORE the audio is read
        or forwarded anywhere, so a refused request never puts a byte of someone's
        voice in front of the provider.
      * RECORD. The exact wording agreed to is stored verbatim with a timestamp,
        so an audit shows what was consented rather than merely that something was.

    Sharing is NOT set here. The row is created unshared and the caregiver makes
    that decision separately (step 5), because collapsing "I recorded this" into
    "I published this to someone" would mean the second decision was never made.

    Args:
        request: The incoming request; the session identifies the owner.
        consent: Must be the string "true". Multipart carries no JSON booleans,
            so this is compared explicitly rather than coerced — `bool("false")`
            is True in Python, and a truthiness check here would be a consent
            bypass in one character.
        display_name: Optional label; defaults to the caller's username.
        samples: The recorded passages.

    Returns:
        `{ok, voice}` where `voice` is the owner projection, always unshared.

    Raises:
        HTTPException: 401/403 for the role gate, 400 without consent or audio,
            413 if the upload exceeds the ceilings, 502 if the provider refused.
    """
    user = _require_caregiver(request)

    # THE CONSENT CHECK COMES FIRST, before the uploads are read. A request
    # without consent must never result in this person's voice being transmitted
    # anywhere, not even to be discarded afterwards.
    if consent.strip().lower() != "true":
        raise HTTPException(
            status_code=400,
            detail="Recording a voice requires the consent statement to be agreed.",
        )

    if not samples:
        raise HTTPException(status_code=400, detail="No recordings were received.")
    if len(samples) > _MAX_SAMPLES:
        raise HTTPException(status_code=413, detail="Too many recordings.")

    blobs: list[tuple[str, bytes]] = []
    total = 0
    for i, upload in enumerate(samples):
        data = await upload.read()
        total += len(data)
        # Bounded per file AND in aggregate: either alone is bypassable by
        # splitting or by sending one enormous part.
        if len(data) > _MAX_SAMPLE_BYTES or total > _MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Recording is too large.")
        if not data:
            continue
        # The filename is generated, never taken from the upload. A
        # client-supplied name reaches a third-party API and a log line, and is
        # a path-traversal and log-injection surface for nothing in return.
        blobs.append((f"passage-{i + 1}.webm", data))

    if not blobs:
        raise HTTPException(status_code=400, detail="The recordings were empty.")

    label = (display_name.strip() or f"{user.username}'s voice")[:64]
    provider_voice_id, error = await voice.clone_supporter_voice(label, blobs)
    if not provider_voice_id:
        # 502: the failure is upstream, not in the caller's request. The error
        # text comes from app/voice.py, which never includes the API key.
        raise HTTPException(status_code=502, detail=error or "Could not create voice.")

    row = voice_store.create_supporter_voice(
        id=uuid.uuid4().hex,
        # Server-side, from the session. Never from the body — there is no field
        # for it to arrive in.
        caregiver_user_id=user.id,
        voice_id=provider_voice_id,
        display_name=label,
        # Stored verbatim. The client sends no wording of its own: it could
        # otherwise record a consent statement nobody ever agreed to.
        consent_text=voice_store.CONSENT_TEXT,
        consented_at=_now(),
        created_at=_now(),
    )
    return {"ok": True, "voice": _owner_voice(row)}


# ---------------------------------------------------------------------------
# Sharing — the caregiver's second, separate decision
# ---------------------------------------------------------------------------
@router.post("/api/voice/share")
async def voice_share(request: Request, body: dict = Body(...)) -> dict:
    """Turn sharing with the linked member on or off.

    Step 5, and deliberately its own endpoint rather than a flag on the clone
    call. A supporter who has recorded a voice has not thereby decided that
    someone should hear it, and the UI reflects that by only offering this
    control after the clone succeeds.

    Only the OWNER may share. A member cannot share a voice to themselves, which
    would turn the caregiver's decision into a formality.

    Args:
        request: The incoming request.
        body: `{id, shared}`. `shared` must be a real boolean; a missing or
            non-boolean value is rejected rather than coerced, because coercion
            is how "0" or "" ends up meaning "share it".

    Returns:
        `{ok, shared}`.

    Raises:
        HTTPException: 400 for a malformed body, 401/403 for the role gate,
            404 if the voice does not exist or is not this caller's.
    """
    user = _require_caregiver(request)
    row_id = str(body.get("id") or "")
    shared = body.get("shared")
    if not row_id or not isinstance(shared, bool):
        raise HTTPException(status_code=400, detail="id and shared are required.")

    row = voice_store.get_supporter_voice(row_id)
    # 404 rather than 403 when it belongs to someone else: a distinguishable
    # response would let a caller probe which voice ids exist.
    if row is None or row.caregiver_user_id != user.id:
        raise HTTPException(status_code=404, detail="No such voice.")

    voice_store.set_shared(row_id, shared)
    return {"ok": True, "shared": shared}


# ---------------------------------------------------------------------------
# Revocation — from either side
# ---------------------------------------------------------------------------
@router.delete("/api/voice/{row_id}")
async def voice_delete(request: Request, row_id: str) -> dict:
    """Revoke a voice. Permitted to the owning caregiver OR the member it is shared with.

    Consent that cannot be withdrawn is not consent, and BOTH parties hold a
    real interest here. The supporter owns their own voice. The member is the
    person it would speak to, and being able to stop hearing a copy of someone's
    voice must not depend on that someone agreeing — which, in the relationships
    this product serves, is not a safe assumption.

    UPSTREAM FIRST, LOCAL SECOND. `voice.delete_voice()` removes the model at the
    provider before the row goes; §7.2's hardest objection is what happens to
    the model when the relationship ends or the person dies, and deleting only
    our row would leave the actual voice model alive somewhere and make this a
    lie. If the upstream call fails we STILL remove the row — the requester's
    revocation is honoured immediately either way, and a stale provider-side
    model that nothing can address beats leaving a person unable to revoke.
    The partial failure is reported in the response rather than hidden.

    Args:
        request: The incoming request.
        row_id: Our opaque row id.

    Returns:
        `{ok, upstream_deleted}` — `upstream_deleted` is false when the provider
        call did not succeed, including when there is no API key configured.

    Raises:
        HTTPException: 401 with no session, 404 if the voice does not exist or
            the caller is neither its owner nor a member it is shared with.
    """
    user = _require_user(request)
    row = voice_store.get_supporter_voice(row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such voice.")

    is_owner = row.caregiver_user_id == user.id
    # The member side: shared, and to a caregiver this member is actually linked
    # to. Both halves required — an unshared voice is not the member's to revoke
    # because it was never offered to them.
    is_recipient = row.shared and store.is_linked(row.caregiver_user_id, user.id)
    if not (is_owner or is_recipient):
        raise HTTPException(status_code=404, detail="No such voice.")

    upstream = await voice.delete_voice(row.voice_id)
    voice_store.delete_supporter_voice(row_id)
    return {"ok": True, "upstream_deleted": upstream}


async def purge_voices_for_user(user_id: str) -> int:
    """Destroy every voice model this account owns. Called on account deletion.

    A hard constraint of the feature: deleting an account deletes the voice
    model. `store.delete_user_data()` cannot do this — it is a pure persistence
    module with no network access, and removing the row while the model lived on
    at the provider would make /data-deletion's promise false in the one place
    it is hardest to notice.

    Upstream first, then local, per voice, for the same reason as the revoke
    route. Failures upstream do not block local removal: the account is leaving
    and must be able to.

    Args:
        user_id: The account being deleted.

    Returns:
        How many rows were removed, so the caller can log THAT a purge happened
        without logging anything about whose voice it was.
    """
    rows = voice_store.voices_for_user(user_id)
    for row in rows:
        await voice.delete_voice(row.voice_id)
        voice_store.delete_supporter_voice(row.id)
    return len(rows)


async def purge_voices_for_link(caregiver_user_id: str, member_user_id: str) -> int:
    """Destroy voices shared through a caregiver link that is being removed.

    The other hard constraint: deleting a caregiver link deletes the voice.
    §7.2's unanswered objection is what happens to a voice model when the
    relationship ends, and the honest answer this build can give is "it stops
    existing". Leaving a shared clone alive after the link is cut would mean the
    member had revoked the relationship but not the voice.

    Only voices that were ACTUALLY SHARED through this link are destroyed. A
    caregiver who recorded a voice and never shared it keeps it: they did not
    make it part of this relationship, and severing the link is not a reason to
    reach into their account and delete their own property.

    Args:
        caregiver_user_id: The caregiver side of the link being removed.
        member_user_id: The member side.

    Returns:
        How many voice models were destroyed.
    """
    if not store.is_linked(caregiver_user_id, member_user_id):
        return 0
    destroyed = 0
    for row in voice_store.list_for_caregiver(caregiver_user_id):
        if not row.shared:
            continue
        await voice.delete_voice(row.voice_id)
        voice_store.delete_supporter_voice(row.id)
        destroyed += 1
    return destroyed


# ---------------------------------------------------------------------------
# What the member may hear
# ---------------------------------------------------------------------------
@router.get("/api/voice/available")
async def voice_available(request: Request) -> dict:
    """Voices the signed-in member may choose from.

    Stock narrators plus any supporter voice shared with them by a caregiver
    they are linked to. Browser speech is NOT listed here: it is not a cloud
    voice, it is always available, and it is the DEFAULT — the client shows it
    as the pre-selected option and never needs the server's permission to use
    it. That is what "default off" means at this step.

    An unshared voice is invisible here even to a member whose caregiver owns
    it. There is no code path that returns one.

    Returns:
        `{online, default_is_browser, stock[], supporter[]}`. `stock` is empty
        when the provider is offline, which the UI reports honestly rather than
        showing an empty picker that looks broken.
    """
    user = _require_user(request)

    stock = [v for v in await voice.list_voices() if not v.get("cloned")]
    supporter = [
        _public_voice(v) for v in voice_store.list_shared_with_member(user.id)
    ]
    return {
        "online": voice.is_online(),
        # Stated by the server so the client cannot drift from the policy:
        # the app speaks in the browser's own voice until the member picks
        # otherwise.
        "default_is_browser": True,
        "stock": stock,
        "supporter": supporter,
    }


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
@router.post("/api/voice/speak")
async def voice_speak(request: Request, body: dict = Body(...)) -> Response:
    """Speak text server-side and return audio/mpeg.

    SERVER-SIDE FOR ONE REASON: the provider API key must never reach a browser.
    The client sends text and receives bytes; no credential appears in any
    response, and the CSP keeps `connect-src` on 'self' so the page could not
    call the provider directly even if it tried (`app/security.py`).

    Rate-limited via `_LIMITS` in `app/security.py` — this endpoint costs money
    per call and is the one voice path a signed-in user can invoke in a loop.
    It is NOT on the emergency path: a failure here degrades to the browser's
    own speech synthesis in the client, so throttling it cannot leave anyone
    without spoken guidance. That is the distinction the security module draws
    between endpoints that may be limited and endpoints that may never be.

    THE AUTHORIZATION RULE: a cloned voice may only be spoken to a member it was
    shared with. `voice_store.member_may_use()` is the single predicate, and it
    is checked before any provider call — so a caller who guessed a voice id
    cannot even cause a billable request, let alone hear it.

    NEVER SILENT. A failure returns 503 with a JSON body rather than an empty
    200; the client is required to fall back to `speechSynthesis` on any
    non-audio response. Silence mid-emergency is the one unacceptable outcome
    (`app/voice.py`), and a robotic voice always beats no guidance.

    Args:
        request: The incoming request.
        body: `{text, voice_id?}`. An absent `voice_id` uses the stock narrator.

    Returns:
        `audio/mpeg` bytes, with `X-Voice-Cloned` and `X-Voice-Id` headers so the
        client can apply the AI-recreation label from the response itself rather
        than from what it thinks it asked for.

    Raises:
        HTTPException: 400 for empty text or a presence claim, 401 with no
            session, 403 for a cloned voice not shared with this caller, 503
            when synthesis fails.
    """
    user = _require_user(request)
    text = str(body.get("text") or "").strip()[:_MAX_SPEAK_CHARS]
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to say.")

    requested = str(body.get("voice_id") or "").strip()

    # Is this one of the caller's own shared supporter voices? Resolved from the
    # database, never from a client-supplied "cloned" flag — the label shown to
    # the user must not be something the client can switch off.
    cloned = bool(requested) and voice_store.member_may_use(user.id, requested)

    if requested and not cloned:
        # The requested id is not a supporter voice this member may use. It is
        # either a stock narrator (fine) or someone else's clone (not fine). We
        # cannot tell the two apart without asking the provider, so we fail
        # closed on anything that looks like one of OUR clones and let genuinely
        # stock ids through.
        known = voice_store.get_supporter_voice_ids_all()
        if requested in known:
            raise HTTPException(
                status_code=403,
                detail="That voice has not been shared with you.",
            )

    if cloned:
        # PRD P5. Only cloned voices are held to this: it is impersonation that
        # makes a presence claim harmful, not the words on their own.
        _refuse_presence_claim(text)

    result = await voice.synthesize(text, voice_id=requested or None, cloned=cloned)
    if not result.live or not result.audio:
        # Honest failure. The client falls back to browser speech on seeing this
        # rather than going quiet — see `speak()` in web/js/app.js.
        raise HTTPException(
            status_code=503, detail=result.error or "Voice unavailable."
        )

    return Response(
        content=result.audio,
        media_type="audio/mpeg",
        headers={
            # Carried explicitly so the UI labels every cloned utterance as an
            # AI recreation without inferring it. Non-negotiable, per the brief
            # and app/voice.py.
            "X-Voice-Cloned": "true" if result.cloned else "false",
            "X-Voice-Id": result.voice_id,
            # Someone's synthesized voice is not a thing to leave in a proxy
            # cache, and app/security.py only sets this on /api/ paths via the
            # middleware — restated here so it survives a change there.
            "Cache-Control": "no-store, private",
        },
    )
