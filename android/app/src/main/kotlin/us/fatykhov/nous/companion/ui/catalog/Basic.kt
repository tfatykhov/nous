package us.fatykhov.nous.companion.ui.catalog

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.ClickableText
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.VerticalDivider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import us.fatykhov.nous.companion.core.Block
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.Inline
import us.fatykhov.nous.companion.core.Markdown
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.stringOrNull
import us.fatykhov.nous.companion.data.UrlOpener
import us.fatykhov.nous.companion.ui.ChildSlot
import us.fatykhov.nous.companion.ui.Children
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.str

/** `weight` → flex-grow; the Compose analogue is a fill modifier when > 0. */
private fun Modifier.weightOf(node: Node.Render): Modifier =
    if ((Functions.flexGrow(node.props["weight"]) ?: 0.0) > 0) fillMaxWidth() else this

// ------------------------------------------------------------------ Text

@Composable
fun TextView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val raw = host.text(node.props["text"], node.scope)
    val caption = node.props.str("variant") == "caption"
    val base = if (caption) 13.sp else 15.sp
    val color = if (caption) t.muted else t.text
    Column(modifier = Modifier.weightOf(node), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (b in Markdown.parse(raw)) when (b) {
            is Block.Heading -> {
                val size = when (b.level) { 1 -> 24.sp; 2 -> 20.sp; 3 -> 17.sp; else -> 15.sp }
                InlineText(b.children, TextStyle(color = color, fontSize = size, fontWeight = FontWeight.SemiBold, fontFamily = t.display))
            }
            is Block.Paragraph -> InlineText(b.children, TextStyle(color = color, fontSize = base))
            is Block.ListBlock -> Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                b.items.forEachIndexed { i, item ->
                    Row { Text(if (b.ordered) "${i + 1}. " else "• ", color = color, fontSize = base); InlineText(item, TextStyle(color = color, fontSize = base)) }
                }
            }
            is Block.CodeBlock -> Text(b.value, color = t.text, fontFamily = t.mono, fontSize = 13.sp,
                modifier = Modifier.fillMaxWidth().background(t.surfaceHover, RoundedCornerShape(6.dp)).padding(8.dp))
        }
    }
}

@Composable
private fun InlineText(nodes: List<Inline>, style: TextStyle) {
    val t = LocalNousTheme.current; val host = LocalSurfaceHost.current
    val links = mutableListOf<Pair<IntRange, String>>()
    val text = buildAnnotatedString {
        fun walk(ns: List<Inline>) { for (n in ns) when (n) {
            is Inline.Text -> append(n.value)
            is Inline.Strong -> withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { walk(n.children) }
            is Inline.Em -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { walk(n.children) }
            is Inline.Code -> withStyle(SpanStyle(fontFamily = t.mono, background = t.surfaceHover)) { append(n.value) }
            is Inline.Link -> { val s = length; withStyle(SpanStyle(color = t.accent, textDecoration = TextDecoration.Underline)) { walk(n.children) }; links.add((s until length) to n.href) }
        } }
        walk(nodes)
    }
    if (links.isEmpty()) Text(text, style = style)
    else ClickableText(text, style = style) { off -> links.firstOrNull { off in it.first }?.let { UrlOpener.open(host.graph.appContext, it.second) } }
}

// ------------------------------------------------------------ Layouts

private fun arrangement(v: String?): Arrangement.HorizontalOrVertical = when (v) {
    "center" -> Arrangement.Center; "spaceBetween" -> Arrangement.SpaceBetween
    "spaceAround" -> Arrangement.SpaceAround; "spaceEvenly" -> Arrangement.SpaceEvenly
    else -> Arrangement.spacedBy(10.dp)
}

@Composable
fun ColumnView(node: Node.Render) {
    val align = when (node.props.str("align")) { "center" -> Alignment.CenterHorizontally; "end" -> Alignment.End; else -> Alignment.Start }
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = arrangement(node.props.str("justify")), horizontalAlignment = align) {
        Children(node.props["children"], node)
    }
}

@Composable
fun RowView(node: Node.Render) {
    val align = when (node.props.str("align")) { "center" -> Alignment.CenterVertically; "end" -> Alignment.Bottom; else -> Alignment.Top }
    // A plain Row, not a scroller: a horizontal List/StatRow inside it would be
    // infinite-inside-infinite width, which Compose refuses. The web's flex row
    // simply lets content wrap or overflow; FlowRow is the faithful analogue.
    androidx.compose.foundation.layout.FlowRow(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = arrangement(node.props.str("justify")),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Children(node.props["children"], node)
    }
}

@Composable
fun ListView(node: Node.Render) {
    if (node.props.str("direction") == "horizontal")
        // Bounded by the screen width (never infinite), then scrollable inside — legal even when nested.
        Row(modifier = Modifier.fillMaxWidth().widthIn(max = androidx.compose.ui.platform.LocalConfiguration.current.screenWidthDp.dp).horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) { Children(node.props["children"], node) }
    else Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) { Children(node.props["children"], node) }
}

@Composable
fun CardView(node: Node.Render) {
    val t = LocalNousTheme.current
    Box(modifier = Modifier.weightOf(node).fillMaxWidth().background(t.surface, RoundedCornerShape(8.dp)).border(1.dp, t.border, RoundedCornerShape(8.dp)).padding(14.dp)) {
        ChildSlot(node.props["child"], node)
    }
}

@Composable
fun DividerView(node: Node.Render) {
    val t = LocalNousTheme.current
    if (node.props.str("axis") == "vertical") VerticalDivider(modifier = Modifier.height(24.dp), color = t.border)
    else HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp), color = t.border)
}

// ----------------------------------------------------------- Image/Icon

@Composable
fun ImageView(node: Node.Render) {
    // No image loader in the beta (no vendor deps): show the description as a labelled box.
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val url = host.text(node.props["url"], node.scope); if (url.isEmpty()) return
    val desc = host.text(node.props["description"], node.scope)
    Box(modifier = Modifier.fillMaxWidth().height(120.dp).background(t.surfaceHover, RoundedCornerShape(8.dp)).semantics { contentDescription = desc }, contentAlignment = Alignment.Center) {
        Text(desc.ifEmpty { "image" }, color = t.muted, fontSize = 13.sp)
    }
}

private val ICONS = mapOf(
    "check" to "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
    "close" to "M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
    "warning" to "M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z",
    "error" to "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z",
    "info" to "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z",
    "add" to "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z",
    "star" to "m12 17.27 6.18 3.73-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z",
)
private const val UNKNOWN_ICON = "M11 18h2v-2h-2v2zm1-16C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm0-14c-2.21 0-4 1.79-4 4h2c0-1.1.9-2 2-2s2 .9 2 2c0 2-3 1.75-3 5h2c0-2.25 3-2.5 3-5 0-2.21-1.79-4-4-4z"

@Composable
fun IconView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val raw = host.resolve(node.props["name"], node.scope)
    // A custom icon's `svgPath` is itself a DynamicString: `{svgPath: {path: "/icons/x"}}`
    // is schema-valid, and resolving only the outer object left the binding unread.
    val custom = (raw as? JsonObject)?.get("svgPath")?.let { host.resolve(it, node.scope) }?.stringOrNull
    val path = custom ?: raw?.stringOrNull?.let { ICONS[it] } ?: UNKNOWN_ICON
    val p = remember(path) { runCatching { PathParser().parsePathString(path).toPath() }.getOrNull() ?: Path() }
    androidx.compose.foundation.Canvas(modifier = Modifier.size(20.dp).semantics { contentDescription = raw?.stringOrNull ?: "icon" }) {
        val s = size.minDimension / 24f
        scale(s, s, pivot = androidx.compose.ui.geometry.Offset.Zero) { drawPath(p, t.text) }
    }
}

// --------------------------------------------------------------- Button

@Composable
fun ButtonView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    val failures = host.functions.runChecks(node.props["checks"] as? JsonArray, host.ctx(node.scope))
    // Actions pause while not LIVE: during a reconnect backoff the snapshot on
    // screen may be stale, and an approval submitted against it would be
    // accepted by the server if the card was merely UPDATED rather than
    // resolved. The Inbox already hides itself on ERROR; this is the same
    // rule at the action itself (codex P1).
    val enabled = !busy && failures.isEmpty() && host.store.connection == us.fatykhov.nous.companion.core.Connection.LIVE
    val onClick: () -> Unit = {
        error = ""
        val action = node.props["action"] as? JsonObject
        val fc = action?.get("functionCall") as? JsonObject
        val ev = action?.get("event") as? JsonObject
        if (fc != null) {
            try { host.functions.callFunction(fc.str("call") ?: "", (fc["args"] as? JsonObject) ?: JsonObject(emptyMap()), host.ctx(node.scope)) } catch (_: Exception) { error = "action failed" }
        } else if (ev != null) {
            val name = ev.str("name") ?: ""
            val ctxObj = buildJsonObject { ((ev["context"] as? JsonObject) ?: JsonObject(emptyMap())).forEach { (k, v) -> host.resolve(v, node.scope)?.let { put(k, it) } } }
            busy = true
            scope.launch {
                val r = host.engine.postAction(host.surfaceId, name, node.componentId, ctxObj)
                if (!r.ok) error = r.message
                busy = false
            }
        }
    }
    Column(modifier = Modifier.weightOf(node), verticalArrangement = Arrangement.spacedBy(3.dp)) {
        val label: @Composable () -> Unit = { ChildSlot(node.props["child"], node) }
        when (node.props.str("variant")) {
            "primary" -> Button(onClick = onClick, enabled = enabled, colors = ButtonDefaults.buttonColors(containerColor = t.accent, contentColor = t.onAccent), shape = RoundedCornerShape(8.dp)) { label() }
            "borderless" -> TextButton(onClick = onClick, enabled = enabled) { label() }
            else -> OutlinedButton(onClick = onClick, enabled = enabled, shape = RoundedCornerShape(8.dp), colors = ButtonDefaults.outlinedButtonColors(contentColor = t.text)) { label() }
        }
        if (error.isNotEmpty()) Text(error, color = t.crit, fontSize = 12.sp)
        else failures.firstOrNull()?.message?.let { Text(it, color = t.muted, fontSize = 12.sp) }
    }
}
