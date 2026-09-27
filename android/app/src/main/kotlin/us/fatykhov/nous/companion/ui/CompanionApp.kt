package us.fatykhov.nous.companion.ui

import android.Manifest
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import us.fatykhov.nous.companion.core.Connection
import us.fatykhov.nous.companion.core.Shell
import us.fatykhov.nous.companion.core.SurfaceState
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.push.Notifications
import us.fatykhov.nous.companion.push.ReconcileFlag
import us.fatykhov.nous.companion.push.TokenState

sealed interface Route {
    data object Inbox : Route
    data object Settings : Route
    data class Surface(val id: String) : Route
}

@Composable
fun CompanionApp(graph: AppGraph, route: Route, navigate: (Route) -> Unit) {
    val version by rememberStoreVersion(graph.store)
    val t = Themes.nousDefault
    // `settings.configured` is a plain file-backed property, invisible to Compose: after a
    // successful Connect the base URL was saved but nothing recomposed, so the Connect screen
    // stayed (on-device report). This state is what flips the shell.
    var configured by remember { mutableStateOf(graph.settings.configured) }
    NousThemed(t) {
        Box(Modifier.fillMaxSize().background(t.bg).safeDrawingPadding()) {
            if (!configured) ConnectScreen(graph) { configured = true; navigate(Route.Inbox) }
            else when (route) {
                Route.Inbox -> InboxScreen(graph, version, navigate)
                Route.Settings -> SettingsScreen(graph, onDisconnect = { configured = false; navigate(Route.Inbox) }) { navigate(Route.Inbox) }
                is Route.Surface -> SurfaceScreen(graph, version, route.id) { navigate(Route.Inbox) }
            }
        }
    }
}

// ------------------------------------------------------------- Connect

/** Material's default field colours are for ITS scheme; on the Nous ground the typed text was near-invisible. */
@Composable
fun fieldColors(): androidx.compose.material3.TextFieldColors {
    val t = LocalNousTheme.current
    return androidx.compose.material3.OutlinedTextFieldDefaults.colors(
        focusedTextColor = t.text, unfocusedTextColor = t.text, disabledTextColor = t.muted,
        cursorColor = t.accent,
        focusedBorderColor = t.accent, unfocusedBorderColor = t.border,
        focusedLabelColor = t.accent, unfocusedLabelColor = t.muted,
        focusedPlaceholderColor = t.muted, unfocusedPlaceholderColor = t.muted,
        focusedContainerColor = t.surface, unfocusedContainerColor = t.surface,
    )
}

@Composable
fun ConnectScreen(graph: AppGraph, onConnected: () -> Unit) {
    val t = LocalNousTheme.current
    var url by remember { mutableStateOf(graph.settings.baseUrl.ifEmpty { "https://" }) }
    var name by remember { mutableStateOf(graph.settings.deviceName) }
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val askNotif = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
        Spacer(Modifier.height(24.dp))
        Text("Nous Companion", color = t.text, fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("Android beta", color = t.accent, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
        Text("Connect to your Nous", color = t.text, fontSize = 30.sp, fontWeight = FontWeight.Bold, lineHeight = 34.sp)
        Text("The app reaches Nous over your tailnet. Keep Tailscale on while you use it.", color = t.soft, fontSize = 15.sp)
        OutlinedTextField(value = url, onValueChange = { url = it }, label = { Text("Nous address") }, modifier = Modifier.fillMaxWidth(), singleLine = true, colors = fieldColors())
        Text("The HTTPS address from `tailscale serve` on the Nous host.", color = t.muted, fontSize = 13.sp)
        OutlinedTextField(value = name, onValueChange = { name = it }, label = { Text("Device name") }, modifier = Modifier.fillMaxWidth(), singleLine = true, colors = fieldColors())
        Button(
            onClick = {
                error = ""; busy = true
                scope.launch {
                    try {
                        val base = url.trim().trimEnd('/')
                        graph.settings.baseUrl = base
                        val probe = runCatching { graph.http.get("/health") }
                        val res = probe.getOrNull()
                        if (res == null || !res.ok) {
                            error = "Couldn't reach Nous at $base" + (res?.let { " (HTTP ${it.status})" } ?: "") + " — check Tailscale and the address." +
                                (probe.exceptionOrNull()?.let { "\n${it.javaClass.simpleName}: ${it.message}" } ?: "")
                            graph.settings.baseUrl = ""
                        } else {
                            graph.settings.deviceName = name.trim().ifEmpty { "Android" }
                            // Everything after the probe is best-effort: a failure here must never block the navigation.
                            runCatching { if (Build.VERSION.SDK_INT >= 33) askNotif.launch(Manifest.permission.POST_NOTIFICATIONS) }
                            runCatching { graph.push.refreshConfig() }
                            runCatching { graph.lifecycle.onStart(androidx.lifecycle.ProcessLifecycleOwner.get()) }
                            onConnected()
                        }
                    } catch (e: Exception) {
                        error = "Connect failed: ${e.javaClass.simpleName}: ${e.message}"
                    } finally { busy = false }
                }
            },
            enabled = !busy && url.startsWith("http"), modifier = Modifier.fillMaxWidth().height(54.dp),
            colors = ButtonDefaults.buttonColors(containerColor = t.accentDim, contentColor = t.onAccent), shape = RoundedCornerShape(14.dp),
        ) { Text(if (busy) "Connecting…" else "Connect", fontSize = 16.sp, fontWeight = FontWeight.SemiBold) }
        if (error.isNotEmpty()) Text(error, color = t.crit, fontSize = 14.sp)
        Text("Nothing is exposed publicly. The web companion keeps working as before.", color = t.muted, fontSize = 13.sp)
    }
}

// --------------------------------------------------------------- Inbox

@Composable
fun InboxScreen(graph: AppGraph, version: Int, navigate: (Route) -> Unit) {
    val t = LocalNousTheme.current
    val feed = remember(version) { graph.store.ordered() }
    val conn = graph.store.connection
    LaunchedEffect(version) {
        // Spec §6.6 reconcile: cancel notifications for surfaces the index no longer lists.
        if (conn == Connection.LIVE) { Notifications.reconcile(graph.appContext, feed.map { it.surfaceId }.toSet()); ReconcileFlag.pending = false }
    }
    val closeAll = remember { Shell.CloseAll { System.currentTimeMillis() } }
    var closeAllArmed by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Nous", color = t.text, fontSize = 22.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            ConnectionPill(conn)
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = { navigate(Route.Settings) }) { Text("Settings", color = t.accent) }
        }
        if (conn == Connection.ERROR) NotConnected(graph) else Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (feed.isEmpty()) Text(if (conn == Connection.LIVE) "Nothing needs you right now. Surfaces Nous pushes — escalations, action reviews, triage — appear here." else "Connecting…", color = t.muted, fontSize = 15.sp, modifier = Modifier.padding(top = 24.dp))
            val micro = feed.filter { Shell.kindOf(it.surfaceId) == "micro_app" }
            for (s in feed) SurfaceRow(graph, s) { navigate(Route.Surface(s.surfaceId)) }
            if (micro.size >= 2) OutlinedButton(onClick = {
                val ids = closeAll.tap(micro.map { it.surfaceId })
                closeAllArmed = ids == null
                if (ids != null) scope.launch { for (id in ids) if (graph.store.surfaces.containsKey(id)) graph.engine.postAction(id, "app.close", "footer", JsonObject(emptyMap())) }
            }, modifier = Modifier.fillMaxWidth()) {
                Text(if (closeAllArmed && closeAll.armed) "sure? close ${closeAll.armedIds.size} apps" else "close all apps (${micro.size})", color = t.accent)
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun ConnectionPill(conn: Connection) {
    val t = LocalNousTheme.current
    val (label, color) = when (conn) {
        Connection.LIVE -> "Live" to t.ok; Connection.CONNECTING -> "Connecting" to t.muted
        Connection.RESYNCING -> "Resyncing" to t.warn; Connection.ERROR -> "Not connected" to t.crit
    }
    Text(label, color = color, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
        modifier = Modifier.border(1.dp, color, RoundedCornerShape(999.dp)).padding(horizontal = 10.dp, vertical = 4.dp))
}

@Composable
private fun SurfaceRow(graph: AppGraph, s: SurfaceState, onOpen: () -> Unit) {
    val t = LocalNousTheme.current
    val kind = Shell.kindOf(s.surfaceId)
    val label = Shell.chipLabel(s, graph.functions)
    val title = s.title.ifEmpty { Shell.chipTooltip(s, graph.functions) }
    Column(Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(14.dp)).border(1.dp, if (s.priority >= 2) t.warn.copy(alpha = 0.6f) else t.border, RoundedCornerShape(14.dp)).clickable(onClick = onOpen).padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(label, color = if (s.priority >= 2) t.warn else t.soft, fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.background(t.surfaceHover, RoundedCornerShape(6.dp)).padding(horizontal = 8.dp, vertical = 3.dp))
            if (s.priority >= 2) Text("needs you", color = t.warn, fontSize = 12.sp)
        }
        Text(title, color = t.text, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Text(kind, color = t.muted, fontSize = 12.sp)
    }
}

@Composable
private fun NotConnected(graph: AppGraph) {
    val t = LocalNousTheme.current
    Column(Modifier.fillMaxWidth().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Spacer(Modifier.height(32.dp))
        Text("Can't reach Nous", color = t.text, fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Text("This phone isn't on your tailnet. Turn on Tailscale, then try again. Notifications still arrive while you're away.", color = t.soft, fontSize = 15.sp)
        Button(onClick = { graph.engine.connect() }, colors = ButtonDefaults.buttonColors(containerColor = t.accentDim, contentColor = t.onAccent), modifier = Modifier.fillMaxWidth().height(54.dp), shape = RoundedCornerShape(14.dp)) { Text("Retry") }
        graph.engine.lastError?.let { Text(it, color = t.muted, fontSize = 12.sp) }
        Text("Cards aren't shown while disconnected, so nothing here is ever out of date.", color = t.muted, fontSize = 13.sp)
    }
}

// ------------------------------------------------------------- Surface

@Composable
fun SurfaceScreen(graph: AppGraph, version: Int, id: String, onBack: () -> Unit) {
    BackHandler(onBack = onBack)
    val s = graph.store.surfaces[id]
    val theme = Themes.byId(s?.theme)
    NousThemed(theme) {
        val t = LocalNousTheme.current
        Column(Modifier.fillMaxSize().background(t.bg)) {
            Row(Modifier.fillMaxWidth().padding(4.dp), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = onBack) { Text("← Inbox", color = t.text) }
                Spacer(Modifier.weight(1f))
                Text(s?.let { Shell.chipLabel(it, graph.functions) } ?: "", color = t.muted, fontSize = 13.sp)
                Spacer(Modifier.width(12.dp))
            }
            if (s == null || graph.store.connection == Connection.RESYNCING) {
                // R16/R17: absent after hydration → not-found; resyncing → nothing presented as current.
                Column(Modifier.fillMaxWidth().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Spacer(Modifier.height(48.dp))
                    if (graph.store.connection == Connection.RESYNCING || graph.store.connection == Connection.CONNECTING) Text("Connecting…", color = t.muted)
                    else {
                        Text("This card is no longer live", color = t.text, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                        Text("Surface not found — it may have resolved or expired. Its notification has been cleared.", color = t.soft, fontSize = 15.sp)
                        LaunchedEffect(id) { Notifications.cancel(graph.appContext, id) }
                    }
                }
            } else {
                val host = remember(id) { SurfaceHost(graph, id) }
                androidx.compose.runtime.CompositionLocalProvider(LocalSurfaceHost provides host) {
                    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
                        @Suppress("UNUSED_EXPRESSION") version
                        Render("root", null, 0, emptyList())
                        Spacer(Modifier.height(32.dp))
                    }
                }
            }
        }
    }
}

// ------------------------------------------------------------ Settings

@Composable
fun SettingsScreen(graph: AppGraph, onDisconnect: () -> Unit = {}, onBack: () -> Unit) {
    BackHandler(onBack = onBack)
    val t = LocalNousTheme.current
    val scope = rememberCoroutineScope()
    var testMsg by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) { TextButton(onClick = onBack) { Text("← Inbox", color = t.text) }; Text("Settings", color = t.text, fontSize = 18.sp, fontWeight = FontWeight.SemiBold) }
        Section("Connection") {
            KV("Nous address", graph.settings.baseUrl); KV("Status", graph.store.connection.name.lowercase()); KV("Device name", graph.settings.deviceName)
        }
        Section("Notifications") {
            KV("Push", graph.push.status); KV("Registered", TokenState.registeredAt?.let { java.text.DateFormat.getTimeInstance().format(it) } ?: "—")
            KV("System permission", if (graph.push.notificationsEnabled()) "Allowed" else "Blocked")
            TokenState.lastError?.let { KV("Last error", it) }
            OutlinedButton(onClick = { scope.launch { graph.push.refreshConfig(); val r = runCatching { graph.http.postJson("/a2ui/push/test", """{"installation_id":"${graph.settings.installationId}"}""") }.getOrNull(); testMsg = if (r?.ok == true) "Test notification sent" else "Not available (${r?.status ?: "offline"})" } }, modifier = Modifier.fillMaxWidth()) { Text("Send test notification", color = t.text) }
            if (testMsg.isNotEmpty()) Text(testMsg, color = t.muted, fontSize = 13.sp)
        }
        Section("Diagnostics") {
            KV("last seq", graph.store.lastSeq.toString()); KV("reconnects", graph.engine.reconnects.toString()); KV("last error", graph.engine.lastError ?: "none")
            KV("installation", graph.settings.installationId.take(8))
        }
        TextButton(onClick = { us.fatykhov.nous.companion.data.UrlOpener.open(graph.appContext, graph.settings.baseUrl + "/companion") }) { Text("Open web companion", color = t.accent) }
        TextButton(onClick = { graph.engine.stop(); graph.settings.clear(); onDisconnect() }) { Text("Disconnect this phone", color = t.crit) }
        Text("Nous Companion " + runCatching { graph.appContext.packageManager.getPackageInfo(graph.appContext.packageName, 0).versionName }.getOrNull(), color = t.muted, fontSize = 12.sp)
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    val t = LocalNousTheme.current
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(title.uppercase(), color = t.muted, fontSize = 12.sp, letterSpacing = 1.sp)
        Column(Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(12.dp)).border(1.dp, t.border, RoundedCornerShape(12.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) { content() }
    }
}

@Composable
private fun KV(k: String, v: String) {
    val t = LocalNousTheme.current
    Row(Modifier.fillMaxWidth()) { Text(k, color = t.muted, fontSize = 14.sp, modifier = Modifier.weight(1f)); Text(v, color = t.text, fontSize = 14.sp, fontFamily = t.mono) }
}
