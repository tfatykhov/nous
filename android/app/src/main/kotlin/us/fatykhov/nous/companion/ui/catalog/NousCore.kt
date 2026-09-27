package us.fatykhov.nous.companion.ui.catalog

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.GNode
import us.fatykhov.nous.companion.core.Graphs
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.stringOrNull
import us.fatykhov.nous.companion.ui.Children
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.str

@Composable
private fun Panel(stripe: Color, content: @Composable () -> Unit) {
    val t = LocalNousTheme.current
    Row(modifier = Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(8.dp)).border(1.dp, t.border, RoundedCornerShape(8.dp))) {
        Box(Modifier.width(3.dp).height(24.dp).background(stripe))
        Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) { content() }
    }
}

@Composable
private fun Badge(text: String, color: Color) {
    Text(text.uppercase(), color = color, fontSize = 11.sp, letterSpacing = 1.sp,
        modifier = Modifier.border(1.dp, color, RoundedCornerShape(999.dp)).padding(horizontal = 8.dp, vertical = 2.dp))
}

@Composable
fun ApprovalPanelView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val title = host.text(node.props["title"], node.scope)
    val summary = host.text(node.props["summary"], node.scope)
    val risk = host.text(node.props["risk"], node.scope)
    Panel(t.warn) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) { Badge("escalation", t.warn); Text(title, color = t.text, fontSize = 17.sp, fontWeight = FontWeight.SemiBold) }
        if (summary.isNotEmpty()) Text(summary, color = t.text, fontSize = 15.sp)
        if (risk.isNotEmpty()) Text("Risk: $risk", color = t.crit, fontSize = 15.sp)
    }
}

@Composable
fun ActionReviewCardView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val comp = host.resolve(node.props["compensation"], node.scope) as? JsonObject
    val revertible = (comp?.get("revertible") as? JsonPrimitive)?.booleanOrNull == true
    Panel(t.accent) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) { Badge("action review", t.accent); Text(host.text(node.props["title"], node.scope), color = t.text, fontSize = 17.sp, fontWeight = FontWeight.SemiBold) }
        for ((k, key) in listOf("Did" to "did", "Why" to "why", "Cost" to "cost")) {
            val v = host.text(node.props[key], node.scope); if (v.isEmpty()) continue
            Text(k, color = t.muted, fontSize = 12.sp); Text(v, color = t.text, fontSize = 15.sp)
        }
        Text("Undo", color = t.muted, fontSize = 12.sp)
        Text(if (revertible) "Revertible." else "Not revertible." + (comp?.str("note")?.let { " $it" } ?: ""), color = if (revertible) t.text else t.muted, fontSize = 15.sp)
    }
}

@Composable
fun StatTileView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val intent = node.props.str("intent")?.takeIf { it in setOf("neutral", "good", "bad", "warn") } ?: "neutral"
    val stripe = when (intent) { "good" -> t.ok; "bad" -> t.crit; "warn" -> t.warn; else -> t.border }
    val valueColor = if (intent == "neutral") t.text else stripe
    Panel(stripe) {
        Text(host.text(node.props["label"], node.scope).uppercase(), color = t.muted, fontSize = 12.sp, letterSpacing = 0.5.sp)
        Text(host.text(node.props["value"], node.scope), color = valueColor, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
        host.text(node.props["delta"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 13.sp) }
    }
}

@Composable
fun StatRowView(node: Node.Render) {
    Row(modifier = Modifier.fillMaxWidth().widthIn(max = androidx.compose.ui.platform.LocalConfiguration.current.screenWidthDp.dp).horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) { Children(node.props["children"], node) }
}

@Composable
fun KeyValueTableView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val (rows, omitted) = Functions.splitTruncation(((host.resolve(node.props["rows"], node.scope) as? JsonArray) ?: JsonArray(emptyList())).toList())
    Column(modifier = Modifier.fillMaxWidth()) {
        rows.forEach { r ->
            val o = r as? JsonObject
            Row(modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(Functions.toDisplayString(o?.get("key")), color = t.muted, fontSize = 14.sp, modifier = Modifier.width(120.dp))
                Text(Functions.toDisplayString(o?.get("value")), color = t.text, fontSize = 14.sp)
            }
        }
        if (omitted != null) Text(Functions.omittedNote(omitted), color = t.muted, fontSize = 12.sp)
    }
}

@Composable
fun DecisionCardView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val outcome = host.text(node.props["outcome"], node.scope)
    val settled = outcome.isNotEmpty() && outcome != "pending"
    val stakes = host.text(node.props["stakes"], node.scope)
    val rawConf = host.resolve(node.props["confidence"], node.scope)
    val conf = ((rawConf as? JsonPrimitive)?.let { if (it.isString) it.content.trim().takeIf { s -> s.isNotEmpty() }?.toDoubleOrNull() else Functions.jsNumberOf(it) })
        ?.takeIf { it.isFinite() && it in 0.0..1.0 }?.let { Math.round(it * 100).toInt() }
    Panel(if (settled) t.border else t.accent) {
        Text(host.text(node.props["description"], node.scope), color = t.text, fontSize = 15.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
            if (stakes.isNotEmpty()) Badge(stakes, if (stakes == "high" || stakes == "critical") t.crit else t.muted)
            host.text(node.props["category"], node.scope).takeIf { it.isNotEmpty() }?.let { Badge(it, t.muted) }
            conf?.let { Badge("$it% conf", t.muted) }
            if (settled) Badge(outcome, if (outcome == "success") t.ok else if (outcome == "failure") t.crit else t.muted)
            Text(host.text(node.props["decisionId"], node.scope).take(8), color = t.muted, fontSize = 12.sp)
        }
    }
}

@Composable
fun ConfidenceMeterView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val v = Functions.jsNumberOf(host.resolve(node.props["value"], node.scope)).let { if (it.isFinite()) it.coerceIn(0.0, 1.0) else 0.0 }
    val band = if (v < 0.4) t.crit else if (v < 0.7) t.warn else t.ok
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        Canvas(modifier = Modifier.weight(1f).height(8.dp)) {
            drawRoundRect(t.border, cornerRadius = androidx.compose.ui.geometry.CornerRadius(4.dp.toPx()))
            drawRoundRect(band, size = size.copy(width = (size.width * v).toFloat()), cornerRadius = androidx.compose.ui.geometry.CornerRadius(4.dp.toPx()))
        }
        Text(Functions.toFixed(v, 2), color = t.text, fontFamily = t.mono, fontSize = 13.sp)
    }
}

@Composable
fun TimelineView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val (rows, omitted) = Functions.splitTruncation(((host.resolve(node.props["items"], node.scope) as? JsonArray) ?: JsonArray(emptyList())).toList())
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        rows.forEach { r ->
            val o = r as? JsonObject
            val flag = (o?.get("flag") as? JsonPrimitive)?.booleanOrNull == true
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(Functions.toDisplayString(o?.get("at")), color = if (flag) t.warn else t.muted, fontFamily = t.mono, fontSize = 12.sp, modifier = Modifier.width(64.dp))
                Column {
                    Text(Functions.toDisplayString(o?.get("label")), color = t.text, fontSize = 14.sp, fontWeight = if (flag) FontWeight.SemiBold else FontWeight.Normal)
                    Functions.toDisplayString(o?.get("detail")).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 13.sp) }
                }
            }
        }
        if (omitted != null) Text(Functions.omittedNote(omitted), color = t.muted, fontSize = 12.sp)
    }
}

@Composable
fun DagGraphView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val nodes = Graphs.dagNodes(host.resolve(node.props["nodes"], node.scope))
    val edges = Graphs.dagEdges(host.resolve(node.props["edges"], node.scope), nodes)
    val layout = Graphs.dagLayout(nodes, edges)
    val density = androidx.compose.ui.platform.LocalDensity.current
    Box(modifier = Modifier.fillMaxWidth().widthIn(max = androidx.compose.ui.platform.LocalConfiguration.current.screenWidthDp.dp).horizontalScroll(rememberScrollState()).background(t.surface, RoundedCornerShape(8.dp)).border(1.dp, t.border, RoundedCornerShape(8.dp)).padding(6.dp)) {
        Canvas(modifier = Modifier.width(with(density) { layout.width.toFloat().toDp() }).height(with(density) { layout.height.toFloat().toDp() })) {
            for (e in edges) {
                val a = layout.pos[e.from] ?: continue; val b = layout.pos[e.to] ?: continue
                drawLine(t.border, Offset((a.x + 12).toFloat(), a.y.toFloat()), Offset((b.x - 14).toFloat(), b.y.toFloat()), strokeWidth = 1.5f)
            }
            for (n in nodes) {
                val p = layout.pos[n.name] ?: continue
                drawCircle(t.token(Graphs.dagStatusToken(n.status)), radius = 10f, center = Offset(p.x.toFloat(), p.y.toFloat()))
            }
        }
        // labels as text overlays are not needed for the beta sweep; names show in the title strip
    }
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        for ((label, tok) in listOf("completed" to "ok", "running" to "accent", "failed" to "crit", "pending" to "muted"))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) { Box(Modifier.width(8.dp).height(8.dp).background(t.token(tok), RoundedCornerShape(4.dp))); Text(label, color = t.muted, fontSize = 11.sp) }
    }
}

@Composable
fun MemoryGraphView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val scope = rememberCoroutineScope()
    var selected by remember { mutableStateOf("") }
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val expanded = remember { mutableSetOf<String>() }
    val nodes = Graphs.graphNodes(host.resolve(node.props["nodes"], node.scope))
    val edges = Graphs.graphEdges(host.resolve(node.props["edges"], node.scope))
    val focus = Graphs.focusId(node.props.str("focusNodeId"), nodes)
    val pos = Graphs.radialLayout(nodes, focus)
    val density = androidx.compose.ui.platform.LocalDensity.current
    // Port of MemoryGraphView.expand: select, call expandGraphNode over /a2ui/call,
    // and merge the returned neighbourhood into the surface's LOCAL data model,
    // deduped so re-expansion and overlapping neighbourhoods never duplicate.
    fun expand(n: GNode) {
        if (busy) return
        selected = n.id
        if (n.id in expanded) return
        busy = true; error = ""
        scope.launch {
            val r = host.engine.callAgentFunction(host.surfaceId, "expandGraphNode", buildJsonObject { put("nodeId", n.id); put("nodeType", n.type ?: "fact") })
            busy = false
            if (!r.ok) { error = r.message; return@launch }
            expanded.add(n.id)
            val value = r.value as? JsonObject ?: return@launch
            val dm = host.surface?.dataModel as? JsonObject ?: return@launch
            val mergedNodes = ((dm["nodes"] as? JsonArray)?.toList() ?: emptyList()).toMutableList()
            val knownN = mergedNodes.mapNotNull { (it as? JsonObject)?.str("id") }.toMutableSet()
            for (el in (value["nodes"] as? JsonArray) ?: JsonArray(emptyList())) { val id = (el as? JsonObject)?.str("id"); if (id != null && knownN.add(id)) mergedNodes.add(el) }
            fun key(e: JsonObject) = "${e.str("source")}→${e.str("target")}:${e.str("relation") ?: ""}"
            val mergedEdges = ((dm["edges"] as? JsonArray)?.toList() ?: emptyList()).toMutableList()
            val knownE = mergedEdges.mapNotNull { (it as? JsonObject)?.let(::key) }.toMutableSet()
            for (el in (value["edges"] as? JsonArray) ?: JsonArray(emptyList())) { val o = el as? JsonObject ?: continue; if (o.str("source") != null && o.str("target") != null && knownE.add(key(o))) mergedEdges.add(el) }
            host.store.patchLocal(host.surfaceId, "/nodes", JsonArray(mergedNodes))
            host.store.patchLocal(host.surfaceId, "/edges", JsonArray(mergedEdges))
        }
    }
    Column(modifier = Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(8.dp)).border(1.dp, t.border, RoundedCornerShape(8.dp)).padding(6.dp)) {
        Canvas(modifier = Modifier.fillMaxWidth().height(with(density) { Graphs.GRAPH_H.toFloat().toDp() })
            .pointerInput(nodes, pos) {
                detectTapGestures { off ->
                    // Nearest node within a finger's radius; the layout is in GRAPH_W×GRAPH_H, x scaled to the canvas.
                    val sx = size.width / Graphs.GRAPH_W.toFloat()
                    val hit = nodes.mapNotNull { n -> pos[n.id]?.let { p -> n to Math.hypot((p.x.toFloat() * sx - off.x).toDouble(), (p.y.toFloat() - off.y).toDouble()) } }
                        .minByOrNull { it.second }?.takeIf { it.second <= 28f * density.density }?.first
                    if (hit != null) expand(hit)
                }
            }) {
            val sx = size.width / Graphs.GRAPH_W.toFloat()
            for (e in edges) {
                val a = pos[e.source] ?: continue; val b = pos[e.target] ?: continue
                drawLine(t.border, Offset(a.x.toFloat() * sx, a.y.toFloat()), Offset(b.x.toFloat() * sx, b.y.toFloat()), strokeWidth = Graphs.edgeStroke(e.weight).toFloat())
            }
            for (n in nodes) {
                val p = pos[n.id] ?: continue
                val r = if (n.id == focus) 14f else 9f
                drawCircle(t.token(Graphs.graphTypeToken(n.type)), radius = r, center = Offset(p.x.toFloat() * sx, p.y.toFloat()))
                if (n.id == selected) drawCircle(t.text, radius = r, center = Offset(p.x.toFloat() * sx, p.y.toFloat()), style = Stroke(2f))
            }
        }
        if (busy) Text("expanding…", color = t.muted, fontSize = 12.sp)
        if (error.isNotEmpty()) Text(error, color = t.crit, fontSize = 12.sp)
    }
}
