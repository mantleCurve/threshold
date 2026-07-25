# Threshold — Product Requirements Document

## 1. Problem statement

Design and build a multimodal, GenAI-powered recovery and prevention platform
that supports individuals navigating substance use disorders and their
caregivers. The solution must use generative AI as a core engine to provide
zero-typing interventions, personalized emergency scripts, educational
resources, and contextual safety tools that empower users and families when
cognitive load is highest.

Threshold is not only a panic button. Most recovery happens before an
emergency, on ordinary days when a missed check-in, craving, lowered tolerance,
or difficult social situation can still be addressed. The product must be
useful across that full arc while remaining immediately usable during an
overdose.

## 2. Product goals

1. Make the next safe action available without typing.
2. Use GenAI to personalize language, context, sequencing, and delivery rather
   than serving the same canned intervention to every person.
3. Support both the person navigating substance use and the caregivers they
   explicitly invite.
4. Reduce cognitive load as risk rises: fewer choices, shorter phrases, larger
   controls, audible guidance, and persistent visual captions.
5. Preserve trust through explicit consent, minimal disclosure, and honest
   reporting of what the system did or could not do.
6. Keep deterministic medical safeguards around the GenAI engine so a model
   failure cannot prevent emergency actions.

## 3. Users and contexts

### 3.1 Member

A person in recovery or at risk of overdose. They may use Threshold while calm,
craving, distressed, intoxicated, panicking, or physically impaired. They own
their profile, caregiver invitations, sharing preferences, and event history.

### 3.2 Caregiver

A family member, friend, sponsor, or other trusted person invited by the member.
They need concise context and immediate next steps, not a surveillance
dashboard. They cannot add themselves to a member’s account.

### 3.3 Bystander

Any person responding to a suspected overdose. Bystander guidance must remain
available without registration or sign-in.

## 4. Core experience

### 4.1 Multimodal, zero-typing interaction

The primary member experience accepts speech through tap-to-start/tap-to-stop and provides
an accessible typed fallback. Responses are delivered simultaneously as:

- concise on-screen text;
- natural spoken narration through ElevenLabs when configured;
- local browser speech when cloud audio is unavailable;
- large visual actions and status changes; and
- contextual prompts derived from the current member profile and recent event
  history.

The microphone is never always-on by default. Spoken output always has a visual
equivalent. A cloud failure must never cause silence.

### 4.2 Six-state escalation ladder

Threshold exposes six understandable states:

0. Baseline
1. Elevated
2. Craving
3. Active use
4. Medical emergency
5. Unresponsive

Generative AI is central to the intervention at each state, but it is not the
sole medical safety classifier. A deterministic rules layer handles explicit
high-risk phrases, sensor events, missed check-ins, manual escalation, and the
non-negotiable Tier 4/5 emergency boundary. This hybrid design keeps AI at the
center of personalization while preventing hallucination or provider downtime
from blocking emergency action.

### 4.3 Prevention and recovery support

Before a crisis, the GenAI engine provides:

- conversational check-ins that respond to the member’s own words;
- personalized craving and grounding interventions;
- refusal and exit scripts written in a tone the member can actually use;
- contextual tolerance-loss education after detox, hospitalization,
  incarceration, or another period of abstinence;
- relevant educational explanations in short, plain language; and
- selection of the most relevant real caregiver recording from the Memory
  Vault.

The product must remain non-judgmental. It does not award sobriety points,
streaks, or badges and does not punish honest disclosure.

### 4.4 Personalized emergency script

The member prepares verified dispatcher facts during onboarding: address, unit,
cross street, entry instruction, naloxone availability, and relevant substances.
GenAI turns those facts and the current situation into a short, readable 112
script while the member is calm.

Safety requirements:

- verified address and entry facts must be preserved character-for-character;
- the script is generated and cached before an emergency whenever possible;
- output is validated before storage;
- missing details are omitted, never invented;
- a deterministic local template is always available as the emergency fallback;
- the script never invents legal protection, dosage, timing, or completed
  emergency actions; and
- the interface presents one short line at a time and can read it aloud.

This is a GenAI feature with deterministic validation and fallback, not a
deterministic-only feature and not an unvalidated model call during a crisis.

### 4.5 Emergency and bystander experience

At Tier 4/5, secondary navigation and nonessential controls leave the screen.
The experience prioritizes:

- one-tap `tel:112` access for India’s Emergency Response Support System;
- the personalized emergency script;
- naloxone and rescue-breathing guidance;
- audible instructions with visible captions;
- location sharing only when needed for the emergency flow;
- caregiver notification to verified, linked caregivers; and
- a clear rescind action for a false alarm.

The interface must never claim that 112 was called, an ambulance was dispatched,
or a caregiver was notified unless the system has a real delivery receipt.

### 4.6 Caregiver support

Members invite caregivers using expiring, single-use codes. Below the emergency
threshold, the member controls which tiers are visible. Tier 4/5 alerts follow
the disclosed emergency policy.

After an emergency event, GenAI creates a concise caregiver brief containing:

- what happened;
- what the system actually completed;
- what to do in the next 60 seconds; and
- supportive, non-confrontational language to use or avoid.

The model may summarize an already-determined event. It may not independently
change the escalation tier or expand the caregiver’s permissions.

## 5. GenAI services and responsibilities

### 5.1 Google Gemini through OpenRouter

Gemini is the primary reasoning and language engine. It is used for:

- live conversational check-ins;
- personalized emergency scripts;
- craving and grounding language;
- refusal and exit scripts;
- tolerance-loss education;
- caregiver situation briefs; and
- contextual Memory Vault selection.

Fast models serve interactive responses. Higher-quality models serve prepared
scripts and caregiver briefs. Every response records whether it was live,
cached, deterministic fallback, or unavailable.

### 5.2 ElevenLabs

ElevenLabs provides natural text-to-speech for zero-typing delivery. Expressive
speech is used for normal interventions and a low-latency voice model is used
for urgent instructions. Stock narration is the default cloud path. Browser
speech remains an explicitly labelled fallback.

Consented caregiver voice cloning is a distinctive Threshold feature. It allows
a member to hear selected interventions in the familiar voice of a trusted
caregiver when reading or typing is difficult. It is separate from the Memory
Vault: Vault messages remain real recordings, while generated guidance may use
an explicitly disclosed synthetic caregiver voice.

The consent chain is mandatory:

1. The caregiver records their own samples while authenticated in their own
   account and accepts the exact cloning consent statement.
2. Creating the voice does not share it. The caregiver separately chooses
   whether to make it available to a linked member.
3. The member explicitly chooses that voice; it is never silently selected.
4. Every synthesized utterance displays and announces that it is an AI
   recreation and that the real caregiver is not live on the line.
5. A cloned voice may not claim presence, awareness, or action by the real
   person, including phrases such as “I am here,” “I am listening,” or “I am on
   my way.”
6. Either participant can revoke access. Revocation deletes the provider-side
   voice model, not only the local database row.
7. Removing the caregiver relationship or deleting the owner’s account also
   removes the shared voice model upstream.

This feature is special because it combines GenAI personalization, multimodal
delivery, and caregiver participation. Its consent and disclosure controls are
part of the feature—not optional policy text.

### 5.3 Non-generative services

Resend delivers email verification codes and emergency caregiver email
notifications. It is infrastructure, not a GenAI service.

## 6. Authentication and onboarding

Member and caregiver registration require:

- full name;
- email address;
- phone number; and
- password.

Email ownership is verified through a short-lived Resend code. Phone number is
required contact information but is not OTP-verified. Usernames are not required
for new accounts.

Onboarding must collect only what the product uses, explain caregiver visibility
before requesting an invite, and allow setup to be completed in a calm moment.

## 7. Privacy, safety, and trust requirements

- The browser never receives OpenRouter, ElevenLabs, or Resend API keys.
- Passwords are stored using a memory-hard password hash.
- Private state-changing endpoints require an authenticated session.
- Caregiver authorization is checked on the server before any event leaves it.
- The member can see their complete event history and active sharing rules.
- AI output never masquerades as a live person or a completed external action.
- Good Samaritan information comes from a reviewed static dataset; a model may
  summarize it only if the exact source text is also available and the summary
  is clearly identified.
- Sensitive prompt content is minimized. Dispatcher-critical profile fields are
  sent only to the emergency-script task.
- Deleting an account removes owned profile data, cached generations, links,
  and associated provider artifacts.

## 8. Accessibility requirements

Target WCAG 2.2 AA:

- complete keyboard navigation and visible focus;
- semantic landmarks, labels, and live regions;
- minimum 44×44 px critical touch targets;
- no meaning communicated by color alone;
- persistent visual captions for all spoken guidance;
- high-contrast emergency controls;
- reduced-motion support;
- responsive layouts at 200% zoom; and
- local text/speech alternatives when microphone, audio, location, network, or
  provider services are unavailable.

## 9. Performance and resilience

- Triage and emergency controls respond without waiting for GenAI.
- Interactive AI responses stream when supported.
- SQLite operations must not block the async event loop.
- Static educational/legal data is loaded once and reused.
- Provider clients reuse connections and enforce bounded timeouts.
- Voice chooses a low-latency model for urgent guidance.
- Failure states are explicit and actionable; no fallback is presented as a
  live generation.

## 10. Success criteria

The judged build is complete when a new visitor can:

1. discover member and caregiver registration from the homepage;
2. register with full name, email, phone, and password;
3. verify email without phone verification;
4. finish member onboarding and generate a caregiver invite;
5. use tap-to-talk to receive a real, personalized GenAI response;
6. generate and hear a validated personalized emergency script;
7. trigger each ladder state and see the interface simplify as risk rises;
8. open bystander overdose guidance without an account;
9. show that a caregiver receives only authorized events and verified emergency
   notifications;
10. see whether every AI response is live, cached, fallback, or offline; and
11. complete the critical flow with keyboard and screen-reader semantics.

## 11. Non-goals

- diagnosing substance use disorder;
- replacing emergency services or clinical care;
- continuous location tracking;
- always-on microphone surveillance;
- sobriety scoring or gamification;
- unreviewed model-generated legal claims;
- hidden caregiver monitoring; or
- claiming a human or emergency service is present when no such connection has
  been confirmed.
