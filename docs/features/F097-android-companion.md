# F097 — Native Android Companion (beta)

> **Status:** 📋 Spec — rev 2, 2026-09-27. Rev 1 was reviewed by a three-person team (protocol/server, Android platform, devil's advocate); every confirmed finding is folded in (§15).
> **Builds on:** [F092](F092-a2ui-companion.md), [F092.1](F092.1-ephemeral-micro-apps.md), [F092.2](F092.2-agent-actions.md), F092.4 (#637), [F093](F093-microapp-design-system.md), [F094](F094-visualization-vocabulary.md), [F096](F096-report-vocabulary.md).

## 1. Summary

A native Android app (Kotlin, Jetpack Compose) that renders the same A2UI surfaces as the web companion at `/companion`, plus **push notifications** via Firebase Cloud Messaging (FCM).

- The web companion keeps its production behaviour and remains the default.
- The native app is an **opt-in beta alternative** until it reaches rendering parity.
- **Access:** the app reaches Nous over the user's Tailscale tailnet (§5). The public web path is unchanged.
- **Not in scope:** a chat client (the companion has no free-text input today), offline-first rendering, iOS.

### 1.1 What changed since F092 Q2

F092 resolved Q2 as "PWA only for v1" and kept Telegram as the only push channel, arguing that a second channel splits notification state. The user has directed both reversals. The design answers the underlying objection structurally:

- **A push is a pointer to a surface, never a record.** When a surface leaves `live` by any path, the server sends a `dismiss` and the phone removes the notification (§6.4).
- **The phone reconciles against the server.** Every hydration cancels notifications for surfaces that are no longer live (§6.6), so a lost dismiss self-heals on the next open.
- **Duplicate alerts are accepted during the beta.** Telegram and push both fire. A "Telegram only as fallback" mode was drafted and **removed** after review: FCM *accepting* a message says nothing about delivery, and a lost or reinstalled phone would silently suppress Telegram for priority-2 approval cards (§15, DA-1).
- **The cost of a second renderer is accepted and bounded:**
  - golden vectors shared by the web and native test suites (§10.1);
  - a hash lock on the web sources that have no vectors (§10.2);
  - a prop-level coverage manifest enforced in the always-on CI (§8.3);
  - a visible "open in web" fallback (§8.2).

## 2. Review of the current approach (2026-09-27)

1. **One notification choke point.** Every companion ping goes through `SurfaceService._notify_telegram` (`nous/a2ui/service.py:1395`), scheduled at `service.py:633` after commit, when `created && should_notify` (priority ≥ 1 or explicit `notify`).
2. **Every end of life is a `live → resolved|expired` write, made by three writers:**
   - `resolve()` (`:881`), which also covers `close` (`:923`), `close_by_dedup_key`, cap eviction (`:877`) and action-driven resolution;
   - `expire_sweep`'s bulk `UPDATE … RETURNING` (`:1078-1087`);
   - `invalidate_heartbeat_surfaces`'s bulk `UPDATE … RETURNING` (`:1165-1172`).

   Dedup replacement emits `deleteSurface`+`createSurface` (`:504-518`) but is **not** an end of life: the row stays `live`, and `created` is false.
3. **The hard parts are rules, not lines.** The web renderer is 2,901 core lines plus 5,113 across 37 adapters. The invariants are listed in §3.2.
4. **Golden inputs exist:**
   - `tests/fixtures/a2ui/examples/*.json` holds **43** upstream conformance examples;
   - `dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json` is a whole report app, CI-locked to its Python builder (`tests/test_a2ui_report.py:466-474`).
5. **Graph layouts are not modules.** The MemoryGraph radial and DagGraph wave layouts live inline in `$derived` blocks inside `MemoryGraphView.svelte` and `DagGraphView.svelte`, and have no tests. Porting them is extraction, not translation (§8.5).
6. **Drift from the docs:**
   - there are six themes, not five (`compose.py:47-57`), and each declares **24** CSS custom properties;
   - `/a2ui/message` (F092 §5.1) was never built;
   - F092 §10.7's per-connection session check is not implemented in `a2ui_stream` (`rest.py:3044-3072`);
   - web push was deliberately absent (`companion-sw.js:16-18`).
7. **`updated_at` is load-bearing.** It has no ORM `onupdate` (`models.py:1340-1342`), and it drives both `_assert_same_epoch` (`actions.py:1389`) and cap-eviction order (`service.py:855-869`). Push code must never write it (§6.4).

## 3. Client contract

### 3.1 Endpoints

| Method | Path | Shape |
|---|---|---|
| GET | `/a2ui/surfaces` | Index `{latest_seq, surfaces:[{surface_id, kind, origin, title, priority, created_at, updated_at}]}`. Never carries nonces. |
| GET | `/a2ui/surfaces/{id}` | One `createSurface` envelope + header `X-A2UI-Upto-Seq`. |
| GET | `/a2ui/stream?since=N` | SSE: `event: a2ui` (`id:` = outbox seq), `event: control` `{"type":"resync"}` (no `id:`), `: keepalive` comments every 15 s of silence. |
| POST | `/a2ui/action` | `{version:"v1.0", action:{name, surfaceId, sourceComponentId, timestamp, context, metadata:{extensions:{com_nous_nonce}}}, a2uiRendererDataModel:{version:"v1.0", surfaces:{<id>: model}}}`. The nonce sits **inside** `action`. |
| POST | `/a2ui/call` | `{version:"v1.0", callAgentFunction:{surfaceId, functionCallId (non-empty), callFunction:{call, args}}, metadata:{extensions:{com_nous_nonce}}}`. The nonce sits at the **top level** (`actions.py:177`, `transport.ts:241-249`). The response body is `{version, agentFunctionResponse:{functionCallId, value \| error}}`. |

Both POSTs require `Content-Type: application/json` (415 otherwise). Push endpoints are in §6.3.

### 3.2 Sync, render and shell rules (ported exactly)

**Sync**
- **R1 — Hydration-first cycle** (`transport.ts:84-165`). In order:
  1. Fetch the index.
  2. Prune surfaces the index omits.
  3. Apply every snapshot as `createSurface`, bypassing dedupe, and record its `X-A2UI-Upto-Seq`.
  4. Set the delivered floor to `latest_seq`.
  5. Open the stream with `?since=` that floor.

  A snapshot 404 aborts the whole cycle into backoff (`transport.ts:107`).
- **R2 — Never auto-resume.** The app uses its own SSE reader over OkHttp, never a library with automatic reconnect, and never sends `Last-Event-ID`: the server prefers that header over `since` (`rest.py:3052`), and a highest-seen id is not a contiguous floor. Any stream error, EOF or read timeout reruns R1 with exponential backoff capped at 30 s (`transport.ts:138-162`). A stream error rehydrates **without** resetting the dedupe cursors (`transport.ts:138-155` vs `167-173`).
- **R3 — Membership dedupe** with a contiguous-prefix floor, never a max watermark (`store.svelte.ts:80-107`).
- **R4 — Per-surface snapshot watermark.** An envelope at or below `upto` is not reapplied. It is still **marked seen** first, so it counts as delivered (`store.svelte.ts:135-143`).
- **R5 — `control/resync`** drops surfaces and cursors and reruns R1. It **preserves** in-flight activity records (`activity`, `doneAt`, `tappedAt`, `stampSeen`; `store.svelte.ts:362-375`).
- **R6 — Nonce** comes only from `createSurface.metadata.extensions.com_nous_nonce`. It rotates on dedup replacement, not on refine.
- **R7 — Model updates.** `updateComponents` merges by id and never deletes. For `updateDataModel`:
  - a missing, empty or `/` path replaces the whole model;
  - otherwise it is an RFC 6901 upsert, and `null` deletes;
  - unescape `~1` before `~0` (`pointer.ts:15`);
  - missing intermediates become an array when the next token is numeric, otherwise an object. That is a local convention shared with `service.py::_pointer_set`, not RFC 6901.

**Interaction**
- **R8 — Actions never throw.** `{error:{code,message}}` renders inline.
- **R9 — Two activity holds (F092.4).**
  - *Model hold:* a 200 from `app.refresh` / `app.refine` holds until the **exact** returned outbox seq is applied, or a snapshot watermark covers it, bounded at 10 s (`activity.ts:72-124`).
  - *Stamp hold:* `app.act` holds until `stampSeen` changes from its value when the record began, or the footer's own timeout expires (`store.svelte.ts:295-305`).
  - `/meta/pendingAction` staleness comes from the stamp's `timeout_s`.
- **R10 — Freshness.** Read `AppHeader.staleAfterS`; 3600 is only the fallback. Timestamps are parsed with `OffsetDateTime`, which accepts both `Z` and `+00:00`, because production emits `+00:00` (`compose.py:312`) and `Instant.parse` rejects offsets on older Android runtimes.
- **R11 — Walker.** Cycle guard by ancestor list, depth cap 64. Unknown, dangling, cycle and depth nodes render inert placeholders and never throw (`Renderer.svelte`).
- **R12 — Templates.** `ChildList {componentId, path}` expands once per array item with a scope. The truncation marker `{_truncated, omitted}` becomes a footer note (`Children.svelte`, `functions.ts:41-70`).
- **R13 — Buttons.**
  - `event` goes to `/a2ui/action`;
  - `functionCall` resolves locally through the basic function table, never the agent RPC;
  - `openUrl` is allowlisted to `https?:` and `mailto:`.
- **R14 — Unknown enum values fall back** to the web default (for example, an unknown `Section.layout` renders as `stack`; `theme.test.ts:81`).

**Shell**
- **R15 — Titles and close-all.**
  - Chip and list titles are never evaluated through effectful functions or `formatString` (`Companion.svelte:85-148`).
  - Close-all is a two-tap arm with a 4 s auto-disarm, and closes only the set the user saw (`Companion.svelte:222-246`).
- **R16 — Surface screen.**
  - It never navigates away on `deleteSurface`, because replacement arrives as delete + create for the same id.
  - A surface absent after hydration shows "Surface not found — it may have resolved or expired" (`Companion.svelte:313`).
- **R17 — Warm resume.** On `ON_START` the store is marked *resyncing*, and actions are disabled until R1 completes. A process that survived in the background must not present old state as current.

**Formatting parity** (JS semantics are the reference; checked by golden vectors, §10.1)
- Figures use `Locale.ROOT`.
- `toFixed(n)` is emulated with `BigDecimal(double)` and `HALF_UP`, the double's exact binary value, so `1.005 → "1.00"` as in V8.
- Integral doubles print without `.0`.
- A documented coercion helper reproduces `Number("") === 0` for the `numeric` check.
- `Intl.PluralRules` / `NumberFormat` / `DateTimeFormat`-backed functions go through a `Formatter` interface: `:app` implements it with `android.icu`, and JVM tests use a documented stand-in.
- Regex checks use `java.util.regex`. Dialect differences from JS are accepted and listed in the coverage manifest.

## 4. Architecture

```
Nous
  push_built (INSERT txn sets push_notified_at when notifying) ─► after commit:
      ├─► Telegram leg (unchanged)
      └─► PushService.notify ─► FCM HTTP v1 (concurrent fan-out, one overall timeout)
  resolve() │ expire_sweep │ invalidate_heartbeat_surfaces  (after commit, if push_notified_at)
      └─► PushService.dismiss ─► FCM (NORMAL)
Android (android/)
  :core  pure Kotlin/JVM — envelopes, SSE parser, SyncEngine (R1–R5), SurfaceStore (R3/R4/R7),
         JSON Pointer, binding + function table, walker (R11/R12/R14), markdown-lite,
         chart/figure/graph geometry, activity/freshness (R9/R10)
  :app   Compose UI, catalog renderers, OkHttp transport, FCM service, token Worker, settings
```

**Split rule:** what to show lives in `:core`, tested on the plain JVM (this machine has a JDK but no Android SDK). How it looks, plus the Android plumbing, lives in `:app`, which CI builds and tests (§11).

## 5. Access — Tailscale (decided by the user, 2026-09-27)

The web companion keeps its public path (Traefik + oauth2-proxy with Google sign-in, unchanged). The native app reaches Nous over the user's **tailnet**:

- **Server.** Tailscale runs on the Nous host, and `tailscale serve` publishes HTTPS on `https://<host>.<tailnet>.ts.net`, proxying to the local Nous port (`127.0.0.1:8383` on prod). MagicDNS and HTTPS certificates must be enabled. There is no Traefik change, no public exposure for the app, and no Nous auth code.
- **Who can connect** is set by tailnet membership plus the tailnet ACL.
- **Phone.** Tailscale's app-based split tunneling (v1.96.2+ has *include* mode) can restrict the VPN to Nous Companion. Only tailnet-addressed traffic uses the tunnel unless an exit node is selected.
- **App.** One base URL, no credentials.
  - Release builds allow **HTTPS only**.
  - Debug builds allow cleartext through a debug-only network security config, for `http://10.0.2.2:8000` (emulator → local Nous).
  - The manifest declares `ACCESS_LOCAL_NETWORK` (enforced on Android 17+ at targetSdk 37). The Connect screen requests it before the first request when the base-URL host resolves to a private, link-local, CGNAT (`100.64/10`) or `.local` address. A connect timeout while it is denied is reported as "local network permission required", not as a network error. Whether Android classifies Tailscale's `100.x` addresses as local is on the device checklist (§10.6).
- **Push is independent of the tunnel.** Nous calls FCM outbound; the phone receives through Google Play services.
- **Limitation:** the app works only while the phone is on the tailnet. Pushes still arrive; tapping one shows "not connected" until Tailscale is up.
- **Exposure note:** the `ts.net` address exposes the whole Nous API to tailnet devices. That is the same exposure the LAN port `:8383` already has.

## 6. Push notifications

### 6.1 Channel and token minting

- FCM HTTP v1 with **data-only** messages, so the app owns display and dismissal.
- OAuth access tokens come from the service-account file via `google-auth` (already a dependency). Minting uses the synchronous transport, so it runs in `asyncio.to_thread`, single-flight under an `asyncio.Lock`, and is cached until shortly before expiry. The loop-stall watchdog exists because of this class of blocking call.
- `PushService` is constructed before the first expiry sweep in `main.py`, because the startup sweep can send dismisses.

### 6.2 Runtime Firebase config (one generic APK; the repo is public)

- **Server mapping.** The server reads the console's `google-services.json` and selects the client whose `android_client_info.package_name == NOUS_A2UI_ANDROID_PACKAGE`:
  - `project_info.project_id` → `project_id`
  - `client_info.mobilesdk_app_id` → `application_id`
  - `api_key[0].current_key` → `api_key`
  - `project_info.project_number` → `sender_id`
- **Server validation.** Before serving `GET /a2ui/push/config`: non-empty project id, app id containing `:`, API key matching `^A[\w-]{38}$`, numeric sender id. On any failure the endpoint returns `{enabled:false, reason}`.
- **The app re-validates** with the same checks before caching or initialising. Malformed options make Firebase Installations throw on FCM's sync thread at every process start.
- **Cache.** A small file in `noBackupFilesDir`, read synchronously in `Application.onCreate` (never `runBlocking` over DataStore).
- **Initialisation.** `FirebaseApp.initializeApp(ctx, options)`, guarded by `FirebaseApp.getApps(ctx).isEmpty()`.
  - The manifest removes `FirebaseInitProvider` (`tools:node="remove"`) and sets `firebase_messaging_auto_init_enabled=false`.
  - The only token fetch is the registration Worker, under try/catch.
  - `Application.onCreate` runs before any service or receiver, so a cold start triggered by an FCM message finds the default app.
- **Changing Firebase projects** takes effect on the next process start; no mismatch UI.
- **No config before first open.** Until the app has been opened once after install it has no Firebase config, so no token and no pushes.
- **API key restriction.** If the Firebase API key carries an Android-app restriction, register the SHA-1 of the beta signing key (§11).

### 6.3 Endpoints and storage

| Method | Path | Body / result |
|---|---|---|
| GET | `/a2ui/push/config` | §6.2 |
| PUT | `/a2ui/push/tokens` | `{installation_id, fcm_token, name, app_version, notifications_enabled}`. Upserts; `fcm_token` is required. |
| DELETE | `/a2ui/push/tokens/{installation_id}` | The only way to deregister. |
| GET | `/a2ui/push/installations` | List for the operator: name, platform, app version, notifications flag, created/updated times, last error. Never the token. |
| POST | `/a2ui/push/test` | `{installation_id}`. Sends a test push; rate-limited by the existing a2ui limiter. |

- **`installation_id`** is a random UUID generated once per install, stored in `noBackupFilesDir`. It is not a credential.
- **Migration `077_a2ui_push.sql`:**
  - creates `nous_system.a2ui_push_installations` with PK `(agent_id, installation_id)` and columns `name`, `platform`, `fcm_token`, `app_version`, `notifications_enabled`, `created_at`, `updated_at`, `last_error`;
  - adds `push_notified_at TIMESTAMPTZ NULL` to `nous_system.a2ui_surfaces`.
- **Bounds (not access control):**
  - at most `NOUS_A2UI_PUSH_MAX_INSTALLATIONS` (10) per agent; a new id beyond the cap gets 409;
  - `name` is at most 40 characters and `app_version` at most 32, with control characters stripped;
  - the first registration of a new `installation_id` sends a Telegram tripwire: "New companion push installation: <name>".

### 6.4 Messages and lifecycle

All data values are strings. `v:"1"`, and `android.restricted_package_name` = the package.

| type | Fields | FCM priority / TTL | Sent when |
|---|---|---|---|
| `surface` | `surface_id, title (≤100 chars), body (≤240 chars; notify_text or ""), priority, kind, expires_at?` | HIGH; TTL = `clamp(expires_at − now, 60 s, 24 h)`, else 24 h | the Telegram condition (`created && should_notify`) |
| `dismiss` | `surface_id` | NORMAL; TTL 28 d | after commit of a terminal transition of a row whose `push_notified_at` is set |
| `test` | `title, body` | HIGH; 1 h | `POST /a2ui/push/test` |

**Intent flag, not acceptance flag.**
- `push_notified_at` is written **in the INSERT transaction** of `push_built` when `created && should_notify` and push is configured. It is never written by a later UPDATE, so `updated_at` is untouched and there is no window in which a fast resolve sees NULL.
- A dismiss sent to a phone that never got the notification is a harmless cancel.
- *Rejected:* stamping after FCM accepts. That leaves permanent notifications for cards resolved during the send; the DAG approval path closes cards within milliseconds (`orchestrator.py:3006-3008`).

**Dismiss sites** (exactly three, each scheduled after commit):
1. `resolve()`;
2. `expire_sweep`'s claim loop (its `RETURNING` gains `push_notified_at`);
3. `invalidate_heartbeat_surfaces` (its `RETURNING` gains `push_notified_at`).

Dedup replacement never dismisses.

**Recipients and errors:**
- Installations with a token **and** `notifications_enabled`. FCM deprioritises apps whose notifications are denied.
- Sends fan out concurrently (`asyncio.gather`) under one overall `NOUS_A2UI_PUSH_TIMEOUT_SECONDS`. "Sent" means **accepted by FCM**; the server cannot observe delivery.
- The token is cleared and `last_error` recorded on `UNREGISTERED`, on `SENDER_ID_MISMATCH`, and on `INVALID_ARGUMENT` **only** when the structured `BadRequest.fieldViolations` names `message.token`. Any other error is a WARNING with no retry: best-effort, like the Telegram leg. The surface is durable, so a lost push loses only the pointer.
- Title and body are truncated at code-point boundaries, and the payload is checked against FCM's 4,096-byte data limit.

### 6.5 Delivery policy

Telegram and FCM both fire, and there is no setting. Duplicate alerts are the accepted cost of the beta. A future "Telegram as fallback" would need a device **ack** (the app confirms it posted the notification, and a delayed Telegram ping fires on no ack). It must never suppress priority-2 cards. It is out of scope here.

### 6.6 Android handling

- **App in the foreground** (`ProcessLifecycleOwner` STARTED): no system notification is posted, because the live UI already shows the surface.
- **`surface`:** ignored if tombstoned. Otherwise `notify(tag = surface_id, id = 1)`:
  - on versioned channels `approvals_v1` (priority 2, high), `updates_v1` (1, default), `general_v1` (0, low);
  - with `setTimeoutAfter(expires_at − now)` when present;
  - the tap is an **explicit** `PendingIntent` to `MainActivity` (`FLAG_IMMUTABLE`), whose data URI `nouscompanion://s/<id>` exists only to make each intent distinct. It is not an exported intent filter; there is no public scheme.
  - `MainActivity` is `singleTop` and handles `onNewIntent`.
- **`dismiss`:** `cancel(surface_id, 1)`, then add a tombstone (persisted, 48 h, at most 500 ids).
- **Reconcile** on every R1 hydration: cancel every active notification whose tag is not in the live index. Also on the next foreground after `onDeletedMessages()`.
- **Tapping a surface that is no longer live** shows R16's "not found" state and cancels the notification.
- The SSE `deleteSurface` event **never** cancels a notification (replacement arrives as delete + create).
- **Token Worker:** `enqueueUniqueWork("push-token", REPLACE)`.
  - It reads `FirebaseMessaging.token` itself, is a no-op without valid cached options, and sends `notifications_enabled` (`areNotificationsEnabled()` and the target channel not `IMPORTANCE_NONE`).
  - It runs on every app start, on resume, and from `onNewToken`. Failures show in Settings diagnostics.
- **Permissions.** `POST_NOTIFICATIONS` is requested at first run; after two denials, Settings deep-links to `ACTION_APP_NOTIFICATION_SETTINGS`.
- **No Google Play services** (`GoogleApiAvailability`): Settings shows push as unavailable, and the app works in the foreground.

### 6.7 Settings (server)

| Variable | Default | Meaning |
|---|---|---|
| `NOUS_A2UI_PUSH_ENABLED` | `true` | Kill switch; inert until both files are configured |
| `NOUS_A2UI_FCM_SERVICE_ACCOUNT_FILE` | `""` | Service-account JSON path inside the container |
| `NOUS_A2UI_FCM_GOOGLE_SERVICES_FILE` | `""` | Firebase `google-services.json` path inside the container |
| `NOUS_A2UI_ANDROID_PACKAGE` | `us.fatykhov.nous.companion` | Must equal the app's `applicationId` |
| `NOUS_A2UI_PUSH_TIMEOUT_SECONDS` | `10` | Overall fan-out bound |
| `NOUS_A2UI_PUSH_MAX_INSTALLATIONS` | `10` | Registration cap per agent |

Each gets a `docker-compose.yml` line with a real default, plus a read-only volume mount for the two files. Operator note: prod's compose file is separate and larger; add the lines there by hand and never copy the repo file over it.

## 7. Android app

### 7.1 Build

| Item | Pin |
|---|---|
| Gradle wrapper | 9.7.1 |
| AGP | 9.3.3 (built-in Kotlin: do **not** apply `org.jetbrains.kotlin.android`) |
| Kotlin (KGP, compose + serialization plugins) | 2.4.20, raised through the root `buildscript` classpath; all plugins declared at root with `apply false` |
| compileSdk / targetSdk / minSdk | 37 / 37 / 26 |
| Compose BOM / Firebase BoM | 2026.09.00 / 34.19.0 |
| OkHttp / kotlinx-serialization / coroutines | 5.5.0 / 1.11.0 / 1.11.0 |
| WorkManager / DataStore / lifecycle-process | 2.12.0 / 1.2.1 / 2.11.0 |
| Robolectric / Roborazzi / JUnit | 4.17 / 1.75.0 / 4.13.2 |
| JDK | CI Temurin 21 (Robolectric at SDK 37 needs 21); locally 25 (`:core` only); bytecode 17 |

- **`:core`** pins `sourceCompatibility`, `targetCompatibility` and `jvmTarget` to 17 with no toolchain, so JDK 25 compiles it.
- **`settings.gradle.kts`** includes `:app` only when an SDK is found:
  - `local.properties` `sdk.dir`, then `ANDROID_HOME`, then `ANDROID_SDK_ROOT`;
  - the `-Pnous.includeApp` property overrides the check;
  - otherwise it logs "No Android SDK: :app excluded".
- **Test tasks** in both modules:
  - set `nous.repoRoot=../..` (relative, so the build cache is machine-independent);
  - declare the catalogs, `tests/fixtures/a2ui` and the F096 fixture as `inputs` with `PathSensitivity.RELATIVE`, so a catalog-only change can never leave a test UP-TO-DATE;
  - `maxHeapSize = 2g`, `org.gradle.jvmargs=-Xmx4g`, `isIncludeAndroidResources = true`.
- **`isMinifyEnabled = false`** for the beta.

### 7.2 Screens

1. **Connect** — base URL (the tailnet `ts.net` URL) and device name. The local-network permission is requested when needed (§5), then the notification permission. A failure says what failed and suggests checking Tailscale.
2. **Inbox** — live surfaces grouped by priority, with kind chips, a connection/resync status line, and close-all per R15.
3. **Surface** — the renderer, themed per surface, following R16.
4. **Settings** — push status, "send test notification", "open in web companion", diagnostics (last seq, reconnect count, last error, Worker status), disconnect.

### 7.3 Lifecycle and networking

- The SSE stream runs only in the foreground. It disconnects on `ON_STOP` with no extra grace: `ProcessLifecycleOwner` already debounces by 700 ms, and a longer timer would race the Android 14+ cached-apps freezer.
- On `ON_START`: R17, then R1.
- **OkHttp clients:**
  - *stream:* `readTimeout` 45 s (3× the 15 s keepalive, which doubles as the watchdog), `callTimeout` 0;
  - *`/a2ui/call`:* `readTimeout` 200 s, since `app.refine` can run up to three 60 s compose rounds;
  - *other REST:* 15 s.
- A `ConnectivityManager.NetworkCallback` (`onLost` / `onAvailable`) cancels the stream call, so R2 reconnects immediately on a network switch.

### 7.4 Manifest and storage

- **Permissions:** `INTERNET`, `POST_NOTIFICATIONS`, `ACCESS_LOCAL_NETWORK`.
- **Messaging service:** `exported="false"` with the `MESSAGING_EVENT` filter. Plus the `FirebaseInitProvider` removal and auto-init meta-data from §6.2.
- **Network security:** the release config has no cleartext; the debug config permits cleartext.
- **Backup:** `allowBackup="false"` plus `dataExtractionRules` excluding both `<cloud-backup>` and `<device-transfer>`. All app state (DataStore file, options cache, `installation_id`, tombstones) lives in `noBackupFilesDir`. Otherwise a restore clones `installation_id` onto a second phone, and the two overwrite each other's token.
- **UI:** edge-to-edge insets through `Scaffold` (enforced from targetSdk 35); predictive back.

## 8. Rendering parity

### 8.1 Coverage target

Everything the web renders:
- **basic, 16 of 18:** Text, Image, Icon, Row, Column, List, Card, Tabs, Modal, Divider, Button, TextField, CheckBox, ChoicePicker, Slider, DateTimeInput;
- **nous-core, 21:** ApprovalPanel, ActionReviewCard, StatTile, KeyValueTable, DecisionCard, ConfidenceMeter, MemoryGraph, DagGraph, AppHeader, AppFooter, Section, StatRow, Timeline, Sparkline, LineChart, BarChart, MetricCard, ScoreCard, DeltaList, DataTable, ChipRow.

Video and AudioPlayer are unimplemented on the web too.

### 8.2 Fallback

An unported component renders a bordered card, "*⟨Name⟩* isn't supported in the Android beta yet", with **Open in web** (`<base>/companion/a/<surface_id>`). It is never blank and never throws.

### 8.3 Coverage manifest (prop-level ratchet)

`android/catalog-coverage.json` declares:
- every catalog component: `ported` or `unsupported`;
- every prop: `handled`, or `ignored` with a reason;
- the handled values of every enum prop;
- every basic-catalog function, with its known dialect deviations.

Two checks enforce it:
1. **A pytest in the always-on `ci.yml`** walks both `catalog.json` files (`properties` / `allOf` / `anyOf` / `oneOf`) and fails on any component, prop, enum value or function the manifest does not cover. A catalog change therefore turns required CI red until the native side acknowledges it; adding `"ignored": "not yet ported"` is the one-line minimum.
2. **A JUnit test** asserts that each Kotlin renderer's declared `handledProps` equals the manifest.

A name-only check would have missed F096, which added props to components that were already registered.

### 8.4 Themes

All 24 custom properties of each of the six theme blocks in `companion.css` are ported to Compose token sets, applied per surface through a `CompositionLocal`. A JVM test parses `companion.css` and asserts every `(theme, token, value)` equals the Kotlin table, which inherits the web's contrast checks (`theme.test.ts:107-140`).

### 8.5 Charts and graphs

- `chart.ts` and `figure.ts` are DOM-free and are ported against golden vectors (§10.1).
- The MemoryGraph and DagGraph layouts are **extracted** from their Svelte views into `:core` geometry. That is its own task in PR 5; it is guarded by the parity lock (§10.2), since the web has no tests to derive vectors from.
- F094/F096 rules carry over:
  - bars zero-based;
  - gaps break lines;
  - lone points drawn as dots;
  - rolling mean computed per finite run;
  - no area fill;
  - tone applied to the judgement, never the value.

## 9. Web companion changes

No production behaviour change. Test-only additions:
- a vitest file that checks the TypeScript implementation against the shared golden vectors, and regenerates their `expected` values when `UPDATE_GOLDEN=1`;
- builder fixtures (§10.3).

## 10. Testing

1. **Golden vectors.**
   - Files: `tests/fixtures/a2ui/golden/{pointer,format,chart,figure,freshness,activity,store,markdown}.json`, each `[{name, input, expected}]`. Inputs come from the existing web test cases; `expected` is generated from the TypeScript.
   - Both sides pin UTC and `en-US`. Floats compare with an epsilon.
   - A parameterized JUnit test per `:core` module reads the files in place.
   - Intl-backed vectors run on vitest and on the device checklist only.
   - The chain: a web change turns vitest red → regenerate → the Android workflow fires → JUnit stays red until Kotlin matches.
2. **Parity lock.** `android/parity-sources.lock` lists the sha256 of every non-test file under `dashboard-app/src/companion/` (`.ts`, `.svelte`, `.css`). A JUnit test fails on any mismatch and names the changed files. `android/scripts/update-parity-lock` re-stamps the lock after the port is reviewed. This covers logic no vector reaches: the walker, graph layouts and shell rules.
3. **Fixture sweep.**
   - Inputs: the 43 examples, the F096 app, and **builder fixtures** — new JSON generated from `nous/a2ui/builders/*` and locked by pytest, as `f096-report-app.json` is.
   - Each must parse and walk with **no placeholder or fallback node** for any component the manifest marks `ported`.
   - Expected text is derived mechanically from each fixture's literal strings, not written by the renderer's author.
4. **`:app` (Robolectric, CI).**
   - Renderers against those fixtures, with the same two assertions over the Compose semantics tree.
   - The messaging handler with `ShadowNotificationManager`: post by tag, cancel by tag, channel mapping, tombstone suppression, foreground suppression, reconcile.
   - A test `Application` keeps Firebase and WorkManager out of unit tests.
   - Roborazzi images are **review artifacts** (record mode), not verification.
5. **Server (pytest, Postgres in CI).**
   - config mapping and validation;
   - token upsert, delete, cap, tripwire;
   - properties of the FCM v1 request (every data value a string, priority ∈ {HIGH, NORMAL}, TTL `^\d+s$`, `restricted_package_name`, ≤ 4,096 bytes), not a hand-written mock's say-so;
   - error mapping: `UNREGISTERED`, `SENDER_ID_MISMATCH` and structured token `INVALID_ARGUMENT` clear the token; an unstructured `INVALID_ARGUMENT` does not;
   - intent flag written in the create transaction;
   - the race: block the fake FCM transport on an `asyncio.Event`, resolve, release, and assert a dismiss was sent;
   - dismiss at all three sites and not on replacement; `updated_at` untouched;
   - the migration.

   `scripts/diag/f097_fcm_validate_only.py` does a `validate_only` dry run once real credentials exist.
6. **On-device checklist** (user-run, recorded in the PR):
   - real delivery with the screen off (Doze);
   - the permission prompts;
   - cold start from cached options;
   - tap → surface;
   - a web-side resolve dismisses the phone notification;
   - Tailscale down → "not connected";
   - on Android 17, whether the `ts.net` address needs the local-network permission;
   - Intl-backed formatting.

## 11. Build, CI, distribution

- **`.github/workflows/android.yml`.**
  - Triggers on `android/**`, `nous/a2ui/catalogs/**`, `tests/fixtures/a2ui/**`, `dashboard-app/src/companion/**` and itself.
  - Uses `actions/setup-java` (Temurin 21) and `gradle/actions/setup-gradle`, with `working-directory: android`.
  - Runs `:core:test :app:testDebugUnitTest :app:recordRoborazziDebug :app:lintDebug :app:assembleDebug`.
  - Uploads the APK and `app/build/outputs/roborazzi/**`, and caches Robolectric's android-all jars.
  - The catalog ratchet lives in `ci.yml` (§8.3), so required CI enforces it; this workflow is path-filtered.
- **Signing.**
  - A beta keystore is generated once with `keytool` and stored as the secrets `ANDROID_BETA_KEYSTORE_B64` and `ANDROID_BETA_KEYSTORE_PASSWORD`. When present, it is the `debug` signingConfig, so each build installs as an update.
  - Without the secrets (fork PRs), a random key is used.
  - **No keystore is ever committed** (the repo is public), and none goes into `actions/cache` (evicted after 7 idle days, which rotates the key silently).
  - No `applicationIdSuffix`, because the server selects the Firebase client by package.
- **Versions.** `versionCode = 1000 + run_number`; `versionName = 0.1.<run>-beta`. A local build (versionCode 1) cannot install over a CI build.
- **Repo hygiene.**
  - `.gitattributes` gains `android/gradlew text eol=lf`, `*.bat text eol=crlf`, `*.jar binary`.
  - `gradlew` is committed with `git add --chmod=+x`.
  - `.gitignore` gains `android/local.properties`, `android/.gradle/`, `android/**/build/`, `*.jks`, `*.keystore`.
- **Locally** only `:core` builds (no SDK here); CI is the gate for `:app`.

## 12. Delivery sequence and acceptance

Rule: every notification a milestone can produce for a **template** surface opens onto a fully rendered surface. Micro-apps show fallback cards until PR 5.

| PR | Scope | Acceptance |
|---|---|---|
| 1 — server push | this spec; migration 077; `PushService`; `/a2ui/push/*`; intent flag; three dismiss sites; settings + compose lines; `validate_only` diag script | pytest green on CI Postgres, including the race test; ruff clean on new code; inert until configured |
| 2 — `:core` | Gradle project, sync engine, store, pointer, binding and function table, walker, markdown-lite, formatting; golden vectors + vitest file; coverage manifest + `ci.yml` pytest (everything `unsupported`); parity lock | `:core` tests pass locally (JDK 25) and in CI; vitest green |
| 3 — app shell + push + templates | Connect, Inbox, Surface, Settings; transport; FCM handler + Worker; every component the six builders emit (ApprovalPanel, ActionReviewCard, DecisionCard, ConfidenceMeter, DagGraph, MemoryGraph, KeyValueTable, StatTile, StatRow, Timeline, plus the basic layout, display and Button components they use) | CI builds the APK; builder fixtures render with zero fallbacks; notification tests green |
| 4 — inputs | TextField, CheckBox, ChoicePicker, Slider, DateTimeInput, Modal, Tabs, with two-way binding | the 43 examples render with zero fallbacks |
| 5 — parity | AppHeader, AppFooter, Section, charts, report vocabulary, graph-layout extraction, F092.4 activity | manifest `unsupported` = {Video, AudioPlayer} |

The beta is accepted when the user completes the on-device checklist (§10.6).

### 12.1 What actually shipped (2026-09-27, PR #659)

All five PRs landed on one branch, in the order 2→3→4→5→1. The plan assumed
no device or emulator was available; one was set up mid-implementation, and
running the app **changed the outcome** — the three defects below were all
invisible to 116 green JVM tests, a CI `:app` job, the prop-level manifest
ratchet and the 43-fixture render sweep, because each is a property of a
runtime none of those load.

| Found on device | Why the suites could not see it |
|---|---|
| `Pattern.UNICODE_CHARACTER_CLASS` crashes the process on Android (ICU rejects the flag) — every ScoreCard | the JVM accepts the flag, so the pure-JVM `:core` is the wrong place to look. The parity premise was also wrong at both ends: JS `\d`/`\w` are ASCII with or without `u`, `\s` is Unicode either way, and the two engines' defaults differ in opposite directions. `JsRegex` spells the classes out; a source-scan test ratchets it |
| Response bodies read on the main thread — hydration never completed | `/health` is small enough to be already buffered, so the Connect probe passed and only the 44 KB snapshots threw |
| `TokenWorker` POSTed to a PUT-only route and forgave the 405 as "no push" | each language's tests prove only that it is self-consistent; nothing compared them. `tests/test_android_push_contract.py` now reads the Kotlin source in the always-on CI and compares **(verb, path) pairs** — a path-only check passes this exact bug |

Two lessons for the next port of this shape:

1. **Stand the platform up before the first delivery, not after a bug report.** The `:core`/`:app` split that makes the rules testable without an SDK is exactly what hides platform-runtime defects; the emulator is not polish, it is the only place that class is observable.
2. **A flag reached for to emulate another language is a smell.** Check what that language does *without* it.

Verified against the live prod Nous over the LAN: all 9 production surfaces
hydrate and render (3 heartbeat triage cards, 6 micro-apps including the F096
health trend report), connection chip **Live**, no logcat crash. 130 Android
tests; 45 push + 12 route + 5 contract tests green on real Postgres; the 245
existing A2UI tests unchanged.

## 13. Risks

| Risk | Mitigation |
|---|---|
| The native renderer drifts behind the web | golden vectors, parity lock, prop-level manifest in required CI, fallback card |
| Duplicate alerts (Telegram + push) | accepted for the beta; §6.5 names the ack-based path |
| Out-of-order or lost FCM messages | tombstones, reconcile on hydration and after `onDeletedMessages`, `setTimeoutAfter`, TTL clamp |
| Anyone who can reach `/a2ui/*` can register an FCM token and receive every future notification's title and body | the same audience can already read full surfaces, so what is exposed doesn't grow; what changes is persistence and channel. Bounded by the installation cap, the Telegram tripwire on each new installation, the list + delete endpoints, `/push/test` rate limiting and field escaping |
| Push payload passes through Google | the same strings the Telegram leg already sends to a third party |
| Malformed Firebase options crash on every start | validated on both server and app before caching; auto-init disabled |
| Backup or device transfer clones `installation_id` | `noBackupFilesDir` plus `dataExtractionRules` |
| JS vs JVM formatting differences | formatting rules in §3.2; golden vectors; the device checklist for Intl |
| No Google Play services | foreground-only use; Settings says so |

## 14. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Kotlin + Compose | first-party Android stack; GenUI is not turnkey at A2UI v1.0 (F092 §12.0) |
| D2 | `:core` pure JVM | rules testable without an SDK; shares vectors with vitest |
| D3 | FCM data-only + runtime Firebase config | the app owns dismissal; one generic APK for a public repo |
| D4 | dismiss gated on an intent flag written at create | race-free by construction; answers F092's split-state objection |
| D5 | no Telegram fallback mode | FCM acceptance ≠ delivery; priority-2 safety |
| D6 | access over Tailscale (user decision) | no public exposure, no auth code, a real TLS certificate |
| D7 | beta signing key in secrets, never committed | public repo: a committed key lets anyone ship an "update" |
| D8 | golden vectors + parity lock + prop-level manifest | parity that turns red on the first divergent change |
| D9 | five-PR sequence | each PR reviewable; notifications never open onto fallbacks for templates |

## 15. Review log (rev 1 → rev 2)

| Reviewer | Verdict | Folded |
|---|---|---|
| Protocol/server | Approve with revisions | 2 P1, 9 P2, 4 P3: pushed_at race, Last-Event-ID trap, `/a2ui/call` body, three dismiss sites + `RETURNING`, `updated_at`, replacement never cancels, stamp hold, R5 precision, graph extraction, `gradlew` eol, 43 fixtures, R4/R7 precision, foreground double-notify |
| Android platform | Approve with revisions | 1 P1, 10 P2, 7 P3: local-network permission + cleartext, OkHttp timeouts, options validation + auto-init, `notifications_enabled`, reconcile + tombstones, backup/transfer, JDK 21 + Roborazzi record mode, declared test inputs, AGP 9 specifics, `gradlew` mode, signing via secrets, manifest checklist, freezer race, Worker details, versions |
| Devil's advocate | Approve with revisions | 2 P1, 12 P2, P3s: fallback removed, intent flag, tombstones/reconcile/dead-surface state, prop-level manifest in `ci.yml`, golden vectors + lock, formatting parity, meaningful smoke tests, PR sequence, SSE timeouts, warm resume, risk row + bounds, off-loop token mint, acceptance honesty, FCM error precision, 24 theme tokens, close-all and title rules, no exported scheme, single deregistration path |

**Overridden:**
- *Commit a throwaway debug keystore* (DA-11): overridden by the platform reviewer's public-repo argument (D7).
- *Extract the web graph layouts into a `.ts` module first:* rejected so the web stays untouched; guarded by the parity lock instead.
