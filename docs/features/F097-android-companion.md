# F097 — Native Android Companion (beta)

> **Status:** 📋 Spec — rev 1, 2026-09-27. Team review pending.
> **Builds on:** [F092](F092-a2ui-companion.md), [F092.1](F092.1-ephemeral-micro-apps.md), [F092.2](F092.2-agent-actions.md), F092.4 (#637), [F093](F093-microapp-design-system.md), [F094](F094-visualization-vocabulary.md), [F096](F096-report-vocabulary.md).

## 1. Summary

A native Android app (Kotlin, Jetpack Compose) that renders the same A2UI surfaces as the web companion at `/companion`, plus **push notifications** via Firebase Cloud Messaging (FCM). The web companion stays unchanged and remains the default. The native app is an **opt-in beta alternative** until it reaches rendering parity.

**Not in scope:**
- A chat client. The companion has no free-text input today.
- Offline-first rendering.
- iOS.
- Any change to web-companion behaviour.

**Open decision, owned by the user:** how the native client authenticates to a Nous deployment (§5). Nothing else in this spec depends on that choice.

### 1.1 What changed since F092 Q2

F092 resolved Q2 as "PWA only for v1" and kept Telegram as the only push channel, on the argument that a second channel splits notification state. The user has directed both reversals: a native app, and push inside it. The reasoning behind the old objection still holds, so the design answers it structurally:

- **A push is a pointer to a surface, never a record.** When a surface leaves `live` by *any* path, the server sends a `dismiss` message and the phone removes the notification (§6.4). Paths include: an action in the web companion, the expiry sweep, heartbeat invalidation, and cap eviction.
- **Duplicate alerts are a setting, not a design flaw.** `NOUS_A2UI_PUSH_TELEGRAM_POLICY=fallback` stops Telegram companion pings whenever a push was delivered (§6.5).
- **The cost of a second renderer is accepted and bounded:**
  - a JVM-testable `:core` that ports the web renderer's rules case-for-case (§10);
  - a catalog-coverage ratchet in CI (§8.3);
  - a visible "open in web" fallback for any component not yet ported (§8.2).

## 2. Review of the current approach (2026-09-27)

1. **A single notification choke point.** Every companion ping goes through `SurfaceService._notify_telegram` (`nous/a2ui/service.py:1395`). It is scheduled once, at `service.py:633`, when a surface is `created` with priority ≥ 1 or explicit `notify`.
2. **Every end of life is a `live → resolved|expired` transition** that emits `deleteSurface`:
   - resolve (`:881`)
   - close (`:923`)
   - expiry sweep (`:1033`)
   - heartbeat invalidation (`:1150`)
   - cap eviction (`:877`)

   Dedup replacement also emits `deleteSurface`+`createSurface`, but it is **not** an end of life: the row stays `live`.
3. **The hard parts are rules, not lines.** The web renderer is about 8,100 production lines: about 2,900 core, plus 5,200 across 37 adapters. The invariants a port must keep exactly are listed in §3.2.
4. **Golden fixtures already exist:**
   - `tests/fixtures/a2ui/examples/*.json` — 37 upstream conformance examples;
   - `dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json` — a whole report app, CI-locked to the Python builder.
5. **Documentation drift:**
   - There are **six** themes, not five (`compose.py:47-57`).
   - `/a2ui/message` from F092 §5.1 was never built.
   - F092 §10.7's per-connection session check on the SSE stream is not implemented in `a2ui_stream` (`rest.py:3044-3072`).
6. **Web push was deliberately absent** (`companion-sw.js:16-18`), so push is new server work, not a port.

## 3. Client contract

### 3.1 Endpoints

| Method | Path | Use |
|---|---|---|
| GET | `/a2ui/surfaces` | Index `{latest_seq, surfaces:[{surface_id, kind, origin, title, priority, created_at, updated_at}]}` — never carries nonces |
| GET | `/a2ui/surfaces/{id}` | Snapshot as one `createSurface` envelope + header `X-A2UI-Upto-Seq` |
| GET | `/a2ui/stream?since=N` | SSE: `event: a2ui` (`id:` = outbox seq), `event: control` `{"type":"resync"}` (no `id:`), `: keepalive` comments |
| POST | `/a2ui/action` | `{version, action:{name, surfaceId, sourceComponentId, timestamp, context, metadata:{extensions:{com_nous_nonce}}}, a2uiRendererDataModel:{version, surfaces:{id: model}}}`; `Content-Type: application/json` required |
| POST | `/a2ui/call` | `callAgentFunction` RPC (`app.refresh`, `app.refine`, `expandGraphNode`, `loadDecisionDetail`); the response body is the `agentFunctionResponse` |

Plus the push endpoints in §6.3.

### 3.2 Sync and render rules (ported exactly; each cites the web source)

- **R1 — Hydration-first cycle** (`transport.ts:84-165`). In order:
  1. Fetch the index.
  2. Prune surfaces the index no longer lists.
  3. Apply every snapshot as `createSurface`, bypassing dedupe, and record `X-A2UI-Upto-Seq`.
  4. Set the delivered floor to `latest_seq`.
  5. Open the stream at that floor.
- **R2 — Never auto-resume.** On any stream error or EOF, rerun R1 with exponential backoff capped at 30 s.
- **R3 — Membership dedupe** with a contiguous-prefix floor, never a max watermark (`store.svelte.ts:80-107`).
- **R4 — Per-surface snapshot watermark.** An envelope at or below `upto` is not reapplied (`store.svelte.ts:109-143`).
- **R5 — `control/resync`** discards local state and reruns R1.
- **R6 — Nonce** comes only from `createSurface.metadata.extensions.com_nous_nonce`. It rotates on replacement, not on refine.
- **R7 — `updateComponents` merges by id and never deletes.** For `updateDataModel`: a missing, empty or `/` path replaces the whole model; otherwise it is an RFC 6901 upsert, and `null` deletes. Unescape `~1` before `~0` (`pointer.ts:15`).
- **R8 — Actions never throw.** Rejections `{error:{code,message}}` render inline.
- **R9 — Activity holds (F092.4).**
  - A 200 from refresh or refine holds until the **exact** returned outbox seq is applied, or a snapshot watermark covers it. The hold is bounded at 10 s (`activity.ts:72-124`).
  - `app.act` staleness comes from the stamp's `timeout_s`.
- **R10 — Freshness.** Read `AppHeader.staleAfterS` from the component; 3600 is only the fallback.
- **R11 — Walker.** Cycle guard by ancestor list, depth cap 64. Unknown, dangling, cycle and depth nodes render inert placeholders and never throw (`Renderer.svelte`).
- **R12 — Templates.** `ChildList {componentId, path}` expands once per array item with a scope; the server truncation marker `{_truncated, omitted}` becomes a footer note (`Children.svelte`, `functions.ts:41-70`).
- **R13 — Buttons.**
  - `event` actions go to `/a2ui/action`.
  - `functionCall` resolves locally through the basic function table, never through the agent RPC.
  - `openUrl` is allowlisted to `https?:` and `mailto:`.

## 4. Architecture

```
Nous: SurfaceService.push_built ─► _notify() ─┬─► Telegram leg (unchanged)
                                              └─► PushService ─► FCM HTTP v1
      terminal transition ─► PushService.dismiss(surface) when pushed_at set
Android (android/):
  :core  pure Kotlin/JVM — envelopes, SSE parser, SyncEngine (R1–R5), SurfaceStore
         (R3/R4/R7), JSON Pointer, dynamic binding + function table, walker (R11/R12),
         markdown-lite, chart + graph geometry, activity/freshness (R9/R10)
  :app   Compose UI, catalog renderers, OkHttp transport, FCM service, settings
```

**Split rule:** anything that decides *what* to show lives in `:core`, tested on the plain JVM. `:app` decides *how* it looks, plus the Android plumbing. This also matches the build environment: this machine has a JDK but no Android SDK, so `:app` is compiled and tested in CI (§11).

## 5. Authentication — OPEN (owner: user)

The web companion is protected by Traefik + oauth2-proxy with Google sign-in, which is a browser login. How the native app authenticates to a deployment is a decision the user makes; this spec does not prescribe it. Until it is decided:

- The app connects to a configured base URL and sends a configurable set of request headers (empty by default). That is the only coupling point.
- Development and CI target a local Nous that has no edge (`http://10.0.2.2:8000` from the emulator, or any LAN URL).
- The push-registration endpoints (§6.3) sit under `/a2ui/` and are protected by exactly what protects the rest of `/a2ui/*`.

## 6. Push notifications

### 6.1 Channel

FCM HTTP v1 with **data-only** messages, so the app owns display and dismissal. The server mints OAuth tokens from a service-account file using `google-auth`, which is already a dependency.

### 6.2 Runtime Firebase config (one generic APK)

- The server reads the Firebase console's `google-services.json` and picks the client entry for `NOUS_A2UI_ANDROID_PACKAGE`.
- It serves the public client identifiers from `GET /a2ui/push/config` as `{enabled, project_id, application_id, api_key, sender_id}`.
- The app calls `FirebaseApp.initializeApp(context, options)` with them, and caches them so `Application.onCreate` can initialise Firebase before `FirebaseMessagingService` runs on a cold start.
- The APK therefore carries no per-deployment Firebase file.
- If the project changes, the app must be restarted.

### 6.3 Endpoints

| Method | Path | Body / result |
|---|---|---|
| GET | `/a2ui/push/config` | as above; `{enabled:false, reason}` when unconfigured |
| PUT | `/a2ui/push/tokens` | `{installation_id, fcm_token, name, app_version}` → upsert; `fcm_token:null` deregisters |
| DELETE | `/a2ui/push/tokens/{installation_id}` | deregister |
| POST | `/a2ui/push/test` | `{installation_id}` → send a test push to that installation |

- `installation_id` is a random UUID the app generates once. It identifies an install for token upsert; it is **not** a credential.
- Storage: migration `077_a2ui_push.sql` creates `nous_system.a2ui_push_installations` with columns `installation_id` PK, `agent_id`, `name`, `platform`, `fcm_token`, `app_version`, `created_at`, `updated_at`, `last_error`. It also adds `pushed_at TIMESTAMPTZ NULL` to `nous_system.a2ui_surfaces`.

### 6.4 Messages

All values are strings; the protocol version field is `v:"1"`.

| type | Fields | FCM android.priority | Sent when |
|---|---|---|---|
| `surface` | `surface_id, title, body, priority, kind` | HIGH, ttl 24 h | the same condition that sends the Telegram ping (`created && should_notify`) |
| `dismiss` | `surface_id` | NORMAL | a `live → resolved/expired` transition of a surface whose `pushed_at` is set |
| `test` | `title, body` | HIGH | `POST /a2ui/push/test` |

- **Content** is the Telegram text: strings already on the card, which the push censor has already checked. The body is truncated to 240 characters to stay under FCM's 4 KB limit.
- **`pushed_at`** is set only when at least one `surface` message is accepted by FCM. Dismisses therefore go only to surfaces that actually produced a notification.
- **Dedup replacement never dismisses**, because the row stays `live`.
- **Invalid tokens** (FCM `UNREGISTERED`, or `INVALID_ARGUMENT` naming the token) clear `fcm_token` and record `last_error`.
- **Other failures** log a WARNING and are not retried, matching the best-effort contract of the Telegram leg. The surface itself is durable, so a lost push loses only the pointer.

### 6.5 Delivery policy

| `NOUS_A2UI_PUSH_TELEGRAM_POLICY` | Behaviour |
|---|---|
| `always` (default while in beta) | Telegram and FCM both fire. |
| `fallback` | FCM first; the Telegram companion ping fires only if FCM delivered to zero installations. |

### 6.6 Android handling

- **`surface`:**
  - Post a notification tagged with `surface_id` on a channel by priority: `approvals` (2, high), `updates` (1, default), `general` (0, low).
  - Tapping opens the surface in the app.
  - Skip the notification if that surface is already on screen.
- **`dismiss`:** cancel the notification by tag. Opening a surface in the app also cancels its notification.
- **Token lifecycle:** `onNewToken`, and every app start, re-registers through WorkManager (network constraint, exponential backoff).
- **Permission:** Android 13+ asks for `POST_NOTIFICATIONS` at first run.

### 6.7 Settings (server)

| Variable | Default | Meaning |
|---|---|---|
| `NOUS_A2UI_PUSH_ENABLED` | `true` | Kill switch; inert until both files below are configured |
| `NOUS_A2UI_FCM_SERVICE_ACCOUNT_FILE` | `""` | Service-account JSON path (mounted into the container) |
| `NOUS_A2UI_FCM_GOOGLE_SERVICES_FILE` | `""` | Firebase `google-services.json` path |
| `NOUS_A2UI_ANDROID_PACKAGE` | `us.fatykhov.nous.companion` | Selects the client entry and must equal the app's `applicationId` |
| `NOUS_A2UI_PUSH_TELEGRAM_POLICY` | `always` | §6.5 |
| `NOUS_A2UI_PUSH_TIMEOUT_SECONDS` | `10` | Per FCM request |

Each needs a `docker-compose.yml` line with a real default.

## 7. Android app

### 7.1 Modules

| Module | Contents |
|---|---|
| `android/core` | Kotlin/JVM: kotlinx-serialization-json (`JsonElement` trees), kotlinx-coroutines |
| `android/app` | `com.android.application`, Compose Material 3, OkHttp, Firebase Messaging, WorkManager, DataStore |

- `minSdk 26`; `targetSdk`/`compileSdk` = the current stable API level.
- Versions are pinned in `gradle/libs.versions.toml`, and the Gradle wrapper is committed.

### 7.2 Screens

1. **Connect** — base URL, optional headers (§5), device name. Connects, then asks for the notification permission.
2. **Inbox** — live surfaces grouped by priority, with kind chips and a connection-status line. "Close all micro-apps" sends `app.close` sequentially, as on the web.
3. **Surface** — the renderer, with themes per surface.
4. **Settings** — push status and "send test notification", "open in web companion", diagnostics (last seq, reconnect count, last error), and disconnect.

### 7.3 Lifecycle

- The SSE stream runs only while the app is in the foreground (`ProcessLifecycleOwner`, disconnecting after a 10 s grace period).
- Background freshness comes from push.
- A cold start shows "connecting…" and never a stale cache presented as current.

### 7.4 Deep links

- `nouscompanion://s/<surface_id>` comes from notifications.
- HTTPS App Links are deferred: Telegram links keep opening the web companion, which is the default client.

## 8. Rendering parity

### 8.1 Coverage target

Every component the web renders:
- **Basic catalog (16 of 18):** Text, Image, Icon, Row, Column, List, Card, Tabs, Modal, Divider, Button, TextField, CheckBox, ChoicePicker, Slider, DateTimeInput.
- **nous-core (21):** ApprovalPanel, ActionReviewCard, StatTile, KeyValueTable, DecisionCard, ConfidenceMeter, MemoryGraph, DagGraph, AppHeader, AppFooter, Section, StatRow, Timeline, Sparkline, LineChart, BarChart, MetricCard, ScoreCard, DeltaList, DataTable, ChipRow.

Video and AudioPlayer are unimplemented on the web too and stay unsupported.

### 8.2 Fallback

An unported component renders a bordered card, "*⟨Name⟩* isn't supported in the Android beta yet", with an **Open in web** button linking to `<base>/companion/a/<surface_id>`. It is never blank and never throws.

### 8.3 Coverage ratchet

An `:app` unit test reads `nous/a2ui/catalogs/*/catalog.json` from the repository and asserts two things:
- every catalog component is in the renderer registry ∪ `UNSUPPORTED`;
- `UNSUPPORTED` ⊆ catalog.

The Android workflow triggers on `nous/a2ui/catalogs/**` too, so a catalog addition fails CI until the native side registers the component or lists it as unsupported, which is a one-line change.

### 8.4 Themes

The six themes become Compose token sets, and their values are copied from `companion.css`:
- colour tokens: `bg`, `surface`, `text`, `muted`, `soft`, `accent`, `ok`, `warn`, `crit`, `locked`, `border`;
- display and numeric font roles.

Themes apply per surface through a `CompositionLocal`, mirroring `data-theme` on each surface root.

### 8.5 Charts and graphs

Port `chart.ts` (DOM-free), `figure.ts`, and the MemoryGraph radial and DagGraph wave layouts into `:core` geometry; Compose `Canvas` draws the results. The F094/F096 normative rules carry over:
- bars zero-based;
- gaps break lines;
- lone points drawn as dots;
- the rolling mean computed per finite run;
- no area fill;
- tone applied to the judgement, never the value.

## 9. Web companion changes

None. The web companion is untouched.

## 10. Testing

1. **`:core` (JVM, runs locally):** port the test **cases** of `store.test.ts`, `pointer.test.ts`, `functions.test.ts`, `activity.test.ts`, `freshness.test.ts`, `chart.test.ts`, `figure.test.ts`, `markdown.test.ts` and the transport-cycle cases of `transport.test.ts`. The same inputs must produce the same outputs.
2. **Fixture sweep:** every `tests/fixtures/a2ui/examples/*.json` and `f096-report-app.json`, read from the repo in place (no copies), must parse and walk with zero unknown components outside `UNSUPPORTED`.
3. **`:app` (CI):**
   - Robolectric + Compose UI smoke tests: every registered component renders its fixture without throwing, and the expected text is present in semantics;
   - the coverage ratchet;
   - Roborazzi screenshots of the fixture gallery, uploaded as CI artifacts for visual review against the web.
4. **Server:** pytest for:
   - config parsing (missing, unreadable, or package absent → `enabled:false` with a reason);
   - token upsert and delete;
   - FCM send through an httpx mock transport (the `UNREGISTERED` path clears the token);
   - notify policy (`always` / `fallback`);
   - `pushed_at` set only after delivery;
   - dismiss on each terminal transition and **not** on dedup replacement;
   - the migration on Postgres (CI).

## 11. Build, CI, distribution

- **Workflow:** `.github/workflows/android.yml`, triggered on `android/**`, `nous/a2ui/catalogs/**`, `tests/fixtures/a2ui/**`, the F096 fixture, and itself. It runs `:core:test`, `:app:testDebugUnitTest`, `:app:lintDebug` and `:app:assembleDebug`, and uploads the APK plus screenshots.
- **Release signing:** when the secrets `ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD` exist, it also builds a signed `assembleRelease`. Without them, the debug APK's signature changes on every run, so an update requires an uninstall.
- **Versions:** `versionCode` = run number; `versionName` = `0.1.<run>-beta`.
- **Local:** only `:core` builds here. `:app` is included only when an Android SDK is found (`local.properties` `sdk.dir` or `ANDROID_HOME`).

## 12. Phasing and acceptance

| Milestone | Scope | Acceptance |
|---|---|---|
| M0 server | migration 077, PushService, `/a2ui/push/*`, notify fan-out + policy, dismiss hooks, settings + compose lines | pytest green on CI Postgres; ruff clean on new code |
| M1 skeleton | `:core` sync/store/binding/walker; app shell (Connect, Inbox, Surface, Settings); FCM receive/notify/dismiss; basic catalog + ApprovalPanel, ActionReviewCard, KeyValueTable, AppHeader, AppFooter, Section, StatTile, StatRow | `:core` tests pass locally; CI builds the APK; Robolectric renders the fixtures |
| M2 parity | the remaining nous-core components incl. charts, graphs, report vocabulary; F092.4 activity parity | `UNSUPPORTED` = {Video, AudioPlayer}; screenshot gallery reviewed |
| M3 promotion (future) | — | real use on device; out of scope here |

The final beta acceptance is an end-to-end run on the user's phone. This environment has no device or emulator, so it cannot be claimed here.

## 13. Risks

| Risk | Mitigation |
|---|---|
| The native renderer drifts behind the web | §8.3 ratchet; §8.2 fallback |
| Duplicate alerts (Telegram + push) | §6.5 `fallback` policy |
| No Google Play services on the device | the app works in the foreground; Settings shows push as unavailable |
| Push payload passes through Google | same strings the Telegram leg already sends to a third party |
| Firebase project changes after first init | the app detects the options mismatch and asks for a restart |
| Local builds cannot compile `:app` | CI is the gate for `:app` (standing practice) |

## 14. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Kotlin + Compose, not Flutter or React Native | first-party Android stack; F092 §12.0's Flutter case rested on GenUI being turnkey, which it is not at A2UI v1.0 |
| D2 | `:core` pure JVM module | rules are testable without an SDK; ports test cases 1:1 |
| D3 | FCM data-only + runtime Firebase config | the app owns dismissal; one generic APK |
| D4 | push = pointer, dismiss on terminal transition | answers F092's split-state objection |
| D5 | web companion untouched | the user's directive: native is an alternative beta |
| D6 | authentication left to the user | §5 |
