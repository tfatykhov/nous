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

@Composable
fun DataTableView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val columns = ((node.props["columns"] as? JsonArray) ?: JsonArray(emptyList())).mapNotNull { it as? JsonObject }.filter { !it.str("key").isNullOrEmpty() }.take(6)
    val (items, omitted) = rows(host, node, "rows")
    val empty = node.props.str("emptyText")?.trim()?.takeIf { it.isNotEmpty() } ?: "no rows"
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Row(Modifier.fillMaxWidth()) { for (c in columns) Text(c.str("label") ?: c.str("key")!!, color = t.muted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, textAlign = if (c.str("align") == "end") TextAlign.End else TextAlign.Start, modifier = Modifier.weight(1f)) }
        HorizontalDivider(color = t.border)
        if (items.isEmpty()) Text(empty, color = t.muted, fontSize = 13.sp)
        for (r in items) Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
            for (c in columns) { val end = c.str("align") == "end"; val sec = (c["secondary"] as? JsonPrimitive)?.content == "true"
                Text(Functions.toDisplayString(r[c.str("key")!!]), color = if (sec) t.muted else t.text, fontSize = 13.sp, fontFamily = if (end) t.mono else t.display.let { androidx.compose.ui.text.font.FontFamily.Default }, textAlign = if (end) TextAlign.End else TextAlign.Start, modifier = Modifier.weight(1f)) }
        }
        Omitted(omitted)
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
