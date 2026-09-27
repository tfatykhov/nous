package us.fatykhov.nous.companion.ui.catalog

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.widthIn
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import us.fatykhov.nous.companion.core.Chart
import us.fatykhov.nous.companion.core.Figure
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.Tone
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.str

/** F096: the VALUE is never coloured; tone lands on the judgement (pill / delta). */
@Composable
private fun TonePill(text: String, tone: Tone) {
    val t = LocalNousTheme.current
    Text(text, color = t.toneInk(tone), fontSize = 11.sp, fontWeight = FontWeight.Bold,
        modifier = Modifier.border(1.dp, t.toneInk(tone), RoundedCornerShape(999.dp)).padding(horizontal = 7.dp, vertical = 1.dp))
}

private fun rows(host: us.fatykhov.nous.companion.ui.SurfaceHost, node: Node.Render, key: String): Pair<List<JsonObject>, Int?> {
    val (kept, omitted) = Functions.splitTruncation(((host.resolve(node.props[key], node.scope) as? JsonArray) ?: JsonArray(emptyList())).toList())
    return kept.mapNotNull { it as? JsonObject } to omitted
}

@Composable
private fun Omitted(omitted: Int?) { if (omitted != null) Text(Functions.omittedNote(omitted), color = LocalNousTheme.current.muted, fontSize = 11.sp) }

private fun unitJoin(value: String, unit: String) = if (unit.isEmpty()) value else if (Figure.isTightUnit(unit)) "$value$unit" else "$value $unit"

@Composable
fun MetricCardView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val tone = Chart.normalizeTone(host.resolve(node.props["tone"], node.scope))   // literal or {path}
    val trendPath = node.props.str("trend")?.trim()?.takeIf { it.isNotEmpty() }
    val series = trendPath?.let { host.resolve(JsonObject(mapOf("path" to JsonPrimitive(it))), node.scope) }?.let { Chart.readSeries(it) }
    Column(modifier = Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(10.dp)).border(1.dp, t.border, RoundedCornerShape(10.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(host.text(node.props["label"], node.scope), color = t.soft, fontSize = 12.sp, modifier = Modifier.weight(1f))
            host.text(node.props["delta"], node.scope).takeIf { it.isNotEmpty() }?.let { TonePill(it, tone) }
        }
        Text(unitJoin(host.text(node.props["value"], node.scope), host.text(node.props["unit"], node.scope)), color = t.text, fontFamily = t.mono, fontSize = 24.sp, fontWeight = FontWeight.Medium)
        host.text(node.props["caption"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 12.sp) }
        if (series != null) { if (series.ok && Chart.classify(Chart.seriesValues(series.points).map { it.v }) != us.fatykhov.nous.companion.core.Degenerate.EMPTY) SparkCanvas(series, tone, (node.props["trendline"] as? JsonPrimitive)?.content == "true", height = 32) else Text(series.reason ?: "no trend data", color = t.muted, fontSize = 11.sp) }
        host.text(node.props["footnote"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 11.sp) }
    }
}

@Composable
fun ScoreCardView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val tone = Chart.normalizeTone(host.resolve(node.props["tone"], node.scope))
    val (items, omitted) = rows(host, node, "items")
    val cardFormat = node.props.str("format")
    val prose = when (cardFormat) { "figure" -> false; "prose" -> true; else -> items.any { r -> val f = r.str("format"); f == "prose" || (f != "figure" && !Figure.isFigureValue(Functions.toDisplayString(r["value"]))) } }
    Column(modifier = Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(10.dp)).border(1.dp, t.border, RoundedCornerShape(10.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(host.text(node.props["title"], node.scope), color = t.text, fontSize = 15.sp, fontWeight = FontWeight.SemiBold, fontFamily = t.display, modifier = Modifier.weight(1f))
            host.text(node.props["status"], node.scope).takeIf { it.isNotEmpty() }?.let { TonePill(it, tone) }
        }
        host.text(node.props["value"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(unitJoin(it, host.text(node.props["unit"], node.scope)), color = t.text, fontFamily = t.mono, fontSize = 22.sp) }
        host.text(node.props["caption"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 12.sp) }
        if (items.isNotEmpty()) HorizontalDivider(color = t.border)
        for (r in items) {
            val ink = t.toneInk(Chart.normalizeTone(r["tone"]))
            if (prose) Column { Text(Functions.toDisplayString(r["label"]), color = t.soft, fontSize = 12.sp); Text(Functions.toDisplayString(r["value"]), color = ink, fontSize = 14.sp) }
            else Row(Modifier.fillMaxWidth()) { Text(Functions.toDisplayString(r["label"]), color = t.soft, fontSize = 13.sp, modifier = Modifier.weight(1f)); Text(Functions.toDisplayString(r["value"]), color = ink, fontFamily = t.mono, fontSize = 13.sp, textAlign = TextAlign.End) }
        }
        Omitted(omitted)
        host.text(node.props["note"], node.scope).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 11.sp) }
    }
}

@Composable
fun DeltaListView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val (items, omitted) = rows(host, node, "rows")
    val empty = node.props.str("emptyText")?.trim()?.takeIf { it.isNotEmpty() } ?: "nothing to report"
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (items.isEmpty()) Text(empty, color = t.muted, fontSize = 13.sp)   // an empty list is a STATE
        for (r in items) {
            val from = Functions.toDisplayString(r["from"]); val to = Functions.toDisplayString(r["to"])
            val range = if (from.isNotEmpty() && to.isNotEmpty()) "$from → $to" else from.ifEmpty { to }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(Functions.toDisplayString(r["label"]), color = t.text, fontSize = 14.sp, modifier = Modifier.weight(1f))
                Text(Functions.toDisplayString(r["delta"]), color = t.toneInk(Chart.normalizeTone(r["tone"])), fontFamily = t.mono, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                if (range.isNotEmpty()) Text(range, color = t.muted, fontFamily = t.mono, fontSize = 12.sp)
            }
        }
        Omitted(omitted)
    }
}

/** The web's `td`/`th` horizontal cell padding (`0.5rem`), which a Compose `Row` has no equivalent of. */
private val CELL_PAD = 6.dp
/** A prose column wraps past this; a figure/date/id column never wraps at all. */
private val PROSE_MAX = 220.dp

@Composable
fun DataTableView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val columns = ((node.props["columns"] as? JsonArray) ?: JsonArray(emptyList())).mapNotNull { it as? JsonObject }.filter { !it.str("key").isNullOrEmpty() }.take(6)
    val (items, omitted) = rows(host, node, "rows")
    val empty = node.props.str("emptyText")?.trim()?.takeIf { it.isNotEmpty() } ?: "no rows"
    val measurer = rememberTextMeasurer()
    val density = LocalDensity.current
    val available = LocalConfiguration.current.screenWidthDp.dp - 32.dp
    val headStyle = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    val proseStyle = TextStyle(fontSize = 13.sp)
    val monoStyle = TextStyle(fontSize = 13.sp, fontFamily = t.mono)
    fun isToken(s: String) = !s.any { it.isWhitespace() }
    // Column widths the way `table-layout: auto` sizes them: each column is as wide
    // as its widest cell, where an end-aligned or single-token cell (a figure, a
    // date, an id) is measured UNWRAPPED and is never broken — equal-weight columns
    // split "0.018581" into "0.0185 / 81" and "(−0.0%)" into "(−0.0% / )", which
    // misread as two values (Bitsgap app on the emulator). A prose cell caps at
    // PROSE_MAX and wraps at spaces. If the columns fit they stretch to fill, like
    // `width: 100%`; if not, the table scrolls inside its own box, like `.scroll`.
    val widths: List<Dp> = remember(items, columns, available) {
        val raw = columns.map { c ->
            val key = c.str("key")!!; val end = c.str("align") == "end"
            var w = measurer.measure(AnnotatedString(c.str("label") ?: key), headStyle, softWrap = false).size.width
            for (r in items) {
                val s = Functions.toDisplayString(r[key])
                val px = measurer.measure(AnnotatedString(s), if (end) monoStyle else proseStyle, softWrap = false).size.width
                val capped = if (end || isToken(s)) px else minOf(px, with(density) { PROSE_MAX.roundToPx() })
                if (capped > w) w = capped
            }
            with(density) { w.toDp() } + CELL_PAD * 2
        }
        val total = raw.fold(0.dp) { a, b -> a + b }
        if (total.value > 0f && total < available) raw.map { it * (available / total) } else raw
    }
    val tableWidth = widths.fold(0.dp) { a, b -> a + b }
    Column(modifier = Modifier.widthIn(max = available).horizontalScroll(rememberScrollState())) {
        Column(modifier = Modifier.width(tableWidth), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row { for ((i, c) in columns.withIndex()) Text(c.str("label") ?: c.str("key")!!, color = t.muted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, softWrap = false, textAlign = if (c.str("align") == "end") TextAlign.End else TextAlign.Start, modifier = Modifier.width(widths[i]).padding(horizontal = CELL_PAD)) }
            HorizontalDivider(color = t.border)
            if (items.isEmpty()) Text(empty, color = t.muted, fontSize = 13.sp)
            for ((ri, r) in items.withIndex()) {
                Row(Modifier.padding(vertical = 5.dp)) {
                    for ((i, c) in columns.withIndex()) {
                        val end = c.str("align") == "end"; val sec = (c["secondary"] as? JsonPrimitive)?.content == "true"
                        val s = Functions.toDisplayString(r[c.str("key")!!])
                        Text(s, color = if (sec) t.muted else t.text, fontSize = 13.sp, fontFamily = if (end) t.mono else androidx.compose.ui.text.font.FontFamily.Default, softWrap = !(end || isToken(s)), textAlign = if (end) TextAlign.End else TextAlign.Start, modifier = Modifier.width(widths[i]).padding(horizontal = CELL_PAD))
                    }
                }
                if (ri < items.lastIndex) HorizontalDivider(color = t.border)   // web: border-bottom on every row but the last
            }
            Omitted(omitted)
        }
    }
}

@Composable
fun ChipRowView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val (items, omitted) = rows(host, node, "items")
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        androidx.compose.foundation.layout.FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            for (r in items) {
                val ink = t.toneInk(Chart.normalizeTone(r["tone"]))
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.background(t.surfaceHover, RoundedCornerShape(999.dp)).border(1.dp, t.border, RoundedCornerShape(999.dp)).padding(horizontal = 10.dp, vertical = 5.dp)) {
                    Text(Functions.toDisplayString(r["label"]), color = t.soft, fontSize = 12.sp)
                    Text(Functions.toDisplayString(r["value"]), color = ink, fontFamily = t.mono, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Functions.toDisplayString(r["detail"]).takeIf { it.isNotEmpty() }?.let { Text(it, color = t.muted, fontSize = 11.sp) }
                }
            }
        }
        Omitted(omitted)
    }
}
