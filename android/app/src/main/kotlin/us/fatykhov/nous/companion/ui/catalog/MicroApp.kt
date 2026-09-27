package us.fatykhov.nous.companion.ui.catalog

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import us.fatykhov.nous.companion.core.Activity
import us.fatykhov.nous.companion.core.ActivityKind
import us.fatykhov.nous.companion.core.ActivityRules
import us.fatykhov.nous.companion.core.FreshnessFormatter
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.Shell
import us.fatykhov.nous.companion.core.stringOrNull
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.str

/** A clock that ticks fast only while something is being counted (web: 1 s working, 30 s idle). */
@Composable
private fun rememberNow(fast: Boolean): Long {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(fast) { while (true) { delay(if (fast) 1000 else 30_000); now = System.currentTimeMillis() } }
    return now
}

private fun metaOf(host: us.fatykhov.nous.companion.ui.SurfaceHost) = (host.surface?.dataModel as? JsonObject)?.get("meta")

/** One definition of "working" (activity.ts): the footer's in-flight record wins, else a fresh server stamp. */
private fun activityOf(host: us.fatykhov.nous.companion.ui.SurfaceHost, nowMs: Long): Activity? =
    host.store.activity[host.surfaceId] ?: ActivityRules.pendingActivity(metaOf(host), nowMs)

// -------------------------------------------------------------- AppHeader

@Composable
fun AppHeaderView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val meta = metaOf(host)
    val pending = ActivityRules.pendingActionOf(meta)
    val activityNow = host.store.activity[host.surfaceId] != null || pending != null
    val now = rememberNow(fast = activityNow)
    val activity = activityOf(host, now)
    val flashing = activity == null && now - (host.store.doneAt[host.surfaceId] ?: 0L) < ActivityRules.DONE_FLASH_MS
    val pendingStale = pending != null && !ActivityRules.pendingIsFresh(pending, now)
    val composedAt = host.text(node.props["composedAt"], node.scope)
    val staleAfterS = (node.props["staleAfterS"] as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toDoubleOrNull() ?: 3600.0   // R10: prop first, 3600 only as fallback
    val freshness = FreshnessFormatter.format(composedAt, now, staleAfterS)
    val note = host.text(node.props["note"], node.scope)
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        // F092.4 rail: a 2dp line on the card's top edge while working.
        if (activity != null) Box(Modifier.fillMaxWidth().height(2.dp).background(t.accent, RoundedCornerShape(2.dp)))
        Text(host.text(node.props["title"], node.scope), color = t.text, fontSize = 22.sp, fontWeight = FontWeight.Bold, fontFamily = t.display)
        host.text(node.props["subtitle"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.soft, fontSize = 14.sp) }
        val (stamp, color) = when {
            activity != null -> "${activity.kind.verb} · ${ActivityRules.formatElapsed(now - activity.startedAt)}" to t.accent
            flashing -> "✓ updated just now" to t.ok
            pendingStale -> "no update after ${ActivityRules.formatElapsed(pending!!.staleMs)}" to t.warn
            else -> freshness.label to (if (freshness.stale) t.warn else t.muted)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(stamp, color = color, fontFamily = t.mono, fontSize = 12.sp, modifier = Modifier.semantics { contentDescription = "stamp" })
            if (note.isNotEmpty()) Text(note, color = t.muted, fontSize = 12.sp)
        }
    }
}

// -------------------------------------------------------------- AppFooter

@Composable
fun AppFooterView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf("") }
    var closing by remember { mutableStateOf(false) }
    val closeArm = remember { Shell.CloseAll { System.currentTimeMillis() } }
    var armed by remember { mutableStateOf(false) }
    // The 4 s expiry lived only in a non-observable getter, so an armed
    // "sure? close" stayed on screen until something else recomposed. Disarm
    // on a timer, like the web's setTimeout (codex P2).
    LaunchedEffect(armed) { if (armed) { delay(Shell.CLOSE_ALL_ARM_MS); armed = false; closeArm.disarm() } }
    val meta = metaOf(host)
    val pending = ActivityRules.pendingActionOf(meta)
    val now = rememberNow(fast = pending != null || host.store.activity[host.surfaceId] != null)
    val pendingFresh = ActivityRules.pendingIsFresh(pending, now)
    val actionError = (meta as? JsonObject)?.get("actionError")?.stringOrNull ?: ""
    val activity = host.store.activity[host.surfaceId]
    val inFlight = activity != null
    val locked = inFlight || closing || pendingFresh
    // The hold bound (web: the footer's own timeout): past HOLD_WAIT_MS the record releases without claiming success.
    LaunchedEffect(activity?.token, activity?.holdSince) {
        val a = activity ?: return@LaunchedEffect
        val since = a.holdSince ?: return@LaunchedEffect
        delay(maxOf(0L, since + ActivityRules.HOLD_WAIT_MS - System.currentTimeMillis()))
        host.store.endActivityIf(host.surfaceId, a.token, false)
    }
    fun opts(key: String): List<Pair<String, String>> {
        val raw = if (key == "refineOptions") host.resolve(node.props[key], null) else node.props[key]
        return ((raw as? JsonArray) ?: JsonArray(emptyList())).mapNotNull { o -> val j = o as? JsonObject; val id = j?.str("id"); val l = j?.str("label"); if (id != null && l != null) id to l else null }
    }
    val refine = opts("refineOptions"); val actions = opts("agentActions")
    val showRefresh = (node.props["showRefresh"] as? JsonPrimitive)?.content != "false"
    fun act(id: String) { if (locked) return; error = ""; val token = host.store.beginActivity(host.surfaceId, ActivityKind.ACT, id)
        scope.launch { var held = false; try { val r = host.engine.postAction(host.surfaceId, "app.act", node.componentId, buildJsonObject { put("actionId", id) }); if (r.ok) held = host.store.holdForStamp(host.surfaceId, token) else error = r.message } finally { if (!held) host.store.endActivityIf(host.surfaceId, token, false) } } }
    fun call(name: String, args: JsonObject, kind: ActivityKind, id: String) { if (locked) return; error = ""; val token = host.store.beginActivity(host.surfaceId, kind, id)
        scope.launch { var held = false; try { val r = host.engine.callAgentFunction(host.surfaceId, name, args); if (r.ok) held = host.store.holdForModel(host.surfaceId, token, ActivityRules.responseSeq(r.value)) else error = r.message } finally { if (!held) host.store.endActivityIf(host.surfaceId, token, false) } } }
    @Composable fun Ctl(label: String, pressed: Boolean, enabled: Boolean, primary: Boolean = false, onClick: () -> Unit) {
        OutlinedButton(onClick = onClick, enabled = enabled, shape = RoundedCornerShape(999.dp)) {
            Text((if (pressed) "… " else "") + label, color = if (pressed) t.accent else if (primary) t.text else t.soft, fontSize = 13.sp)
        }
    }
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        HorizontalDivider(color = t.border)
        androidx.compose.foundation.layout.FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            for ((id, label) in actions) Ctl(label, pressed = (pendingFresh && pending?.id == id) || (activity?.kind == ActivityKind.ACT && activity.id == id), enabled = !locked, primary = true) { act(id) }
            for ((id, label) in refine) Ctl(label, pressed = activity?.kind == ActivityKind.REFINE && activity.id == id, enabled = !locked) { call("app.refine", buildJsonObject { put("id", id) }, ActivityKind.REFINE, id) }
            if (showRefresh) Ctl(if (activity?.kind == ActivityKind.REFRESH) "Refreshing" else "refresh", pressed = activity?.kind == ActivityKind.REFRESH, enabled = !locked) { call("app.refresh", JsonObject(emptyMap()), ActivityKind.REFRESH, "refresh") }
            Ctl(if (armed && closeArm.armed) "sure? close" else "close", pressed = false, enabled = !inFlight && !closing) {
                val ids = closeArm.tap(listOf(host.surfaceId)); armed = ids == null
                if (ids != null) { closing = true; scope.launch { val r = host.engine.postAction(host.surfaceId, "app.close", node.componentId, JsonObject(emptyMap())); if (!r.ok) { error = r.message; closing = false } } }
            }
        }
        if (pending != null && !pendingFresh) Text("\"${pending.label}\" got no update — the agent may have failed; tap again to retry.", color = t.warn, fontSize = 12.sp)
        if (actionError.isNotEmpty()) Text(actionError, color = t.crit, fontSize = 12.sp)
        if (error.isNotEmpty()) Text(error, color = t.crit, fontSize = 12.sp)
    }
}

// ---------------------------------------------------------------- Section

@Composable
fun SectionView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val layout = node.props.str("layout")?.takeIf { it in setOf("stack", "hero", "grid-2", "grid-3", "rail", "accordion", "cards") } ?: "stack"   // R14 fallback
    val model = node.props.str("provenance") == "model"
    val caption = host.text(node.props["caption"], node.scope)
    val now = rememberNow(fast = false)
    val dimmed = host.store.activity[host.surfaceId] != null || ActivityRules.pendingActivity(metaOf(host), now) != null
    var open by remember { mutableStateOf(false) }
    val collapsible = layout == "accordion"
    Column(modifier = Modifier.fillMaxWidth().then(if (model) Modifier.border(1.dp, t.warn, RoundedCornerShape(8.dp)).padding(8.dp) else Modifier).alpha(if (dimmed) 0.72f else 1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        HorizontalDivider(color = t.border)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth().then(if (collapsible) Modifier.clickable { open = !open } else Modifier)) {
            // Both weighted: an unweighted caption is measured FIRST and a long one
            // takes the whole row, leaving the weighted title zero width — which
            // Compose renders as one letter per line ("S/i/g/n/a/l/s", seen on the
            // Bitsgap app). CSS never goes below a word's width; Compose will.
            Text(node.props.str("title") ?: "", color = t.text, fontSize = if (layout == "hero") 20.sp else 15.sp, fontWeight = FontWeight.SemiBold, fontFamily = t.display, modifier = Modifier.weight(1f))
            if (model) Text("model-supplied", color = t.warn, fontSize = 11.sp, modifier = Modifier.border(1.dp, t.warn, RoundedCornerShape(999.dp)).padding(horizontal = 6.dp, vertical = 1.dp))
            if (caption.isNotEmpty()) Text(caption, color = t.muted, fontSize = 12.sp, textAlign = androidx.compose.ui.text.style.TextAlign.End, modifier = Modifier.weight(1.4f))
            if (collapsible) Text(if (open) "▾" else "▸", color = t.muted)
        }
        // A collapsed panel is NOT composed (mirrors Tabs); layouts grid-2/grid-3/cards/rail lay out the child's items —
        // the child is a Column/Row whose items become cells; a FlowRow of the child's children approximates that.
        if (!collapsible || open) node.props.str("child")?.let { Render(it, node.scope, node.depth, node.ancestors) }
    }
}
