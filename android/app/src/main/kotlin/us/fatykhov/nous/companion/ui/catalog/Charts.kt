package us.fatykhov.nous.companion.ui.catalog

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import us.fatykhov.nous.companion.core.Chart
import us.fatykhov.nous.companion.core.Degenerate
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.IV
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.ReadSeries
import us.fatykhov.nous.companion.core.Tone
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.NousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.str

/** F094 §3.1 renderer-owned states: a non-series or empty series says WHY, never a blank box. */
@Composable
private fun SeriesState(series: ReadSeries, kind: Degenerate): Boolean {
    val t = LocalNousTheme.current
    val msg = when {
        !series.ok -> "not a series (${series.shape})"
        kind == Degenerate.EMPTY -> series.reason ?: "no data"
        else -> return false
    }
    Text(msg, color = t.muted, fontSize = 12.sp, modifier = Modifier.semantics { contentDescription = "series-state" })
    return true
}

private fun seriesOf(host: us.fatykhov.nous.companion.ui.SurfaceHost, node: Node.Render): ReadSeries =
    Chart.readSeries(host.resolve(JsonObject(mapOf("path" to JsonPrimitive(node.props.str("path") ?: ""))), node.scope))

/** Segments as "x,y x,y" strings from `:core` (same as the SVG), parsed back into a Path. */
private fun DrawScope.drawSegments(segments: List<String>, color: Color, width: Float, sx: Float, sy: Float) {
    for (seg in segments) {
        val pts = seg.split(' ').map { p -> val (x, y) = p.split(','); Offset(x.toFloat() * sx, y.toFloat() * sy) }
        if (pts.size == 1) drawCircle(color, radius = 2f * sx, center = pts[0])   // a lone reading is a dot: a one-point polyline strokes nothing
        else { val path = Path().apply { moveTo(pts[0].x, pts[0].y); for (p in pts.drop(1)) lineTo(p.x, p.y) }; drawPath(path, color, style = Stroke(width)) }
    }
}

/** The sparkline body shared by Sparkline and MetricCard (web: SparkSvg). */
@Composable
fun SparkCanvas(series: ReadSeries, tone: Tone, trendline: Boolean, height: Int = 40) {
    val t = LocalNousTheme.current
    val w = 260.0; val pad = 6.0
    val finite = Chart.seriesValues(series.points)
    val main = if (trendline) Chart.rollingMean(finite, Chart.trendWindow(series.points.size)) else finite
    val domain = Chart.yDomain((finite.map { it.v } + if (trendline) main.map { it.v } else emptyList()), zeroBase = false)
    val x = Chart.xScale(series.points.size, w, pad); val y = Chart.yScale(domain, height.toDouble(), pad)
    val raw = if (trendline) Chart.lineSegments(finite, x, y) else emptyList()
    val segs = Chart.lineSegments(main, x, y)
    val focus = Chart.focusStartIndex(series.points, series.focusFrom)
    val last = finite.lastOrNull()
    val lastIsolated = finite.size == 1 || (finite.size > 1 && finite[finite.size - 1].i != finite[finite.size - 2].i + 1)
    Canvas(modifier = Modifier.fillMaxWidth().height(height.dp).semantics { contentDescription = "sparkline" }) {
        val sx = size.width / w.toFloat(); val sy = size.height / height.toFloat()
        if (focus != null) drawRect(t.accentGlow, topLeft = Offset(x(focus).toFloat() * sx, 0f), size = size.copy(width = size.width - x(focus).toFloat() * sx))
        if (domain.zeroBreak) drawLine(t.muted, Offset(0f, size.height), Offset(6f * sx, size.height - 4f * sy), strokeWidth = 1f)   // `~` break marker
        drawSegments(raw, t.tone(tone).copy(alpha = 0.35f), 1.5f * sx, sx, sy)
        drawSegments(segs, t.tone(tone), 2f * sx, sx, sy)
        if (last != null && !lastIsolated) drawCircle(t.tone(tone), radius = 3f * sx, center = Offset(x(last.i).toFloat() * sx, y(last.v).toFloat() * sy))   // end dot, skipped when already a lone dot
    }
}

@Composable
fun SparklineView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val series = seriesOf(host, node)
    val values = Chart.seriesValues(series.points).map { it.v }
    val kind = Chart.classify(values)
    val tone = Chart.normalizeTone(node.props["tone"])
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(Functions.toDisplayString(node.props["label"]), color = t.muted, fontSize = 12.sp, modifier = Modifier.weight(1f))
            values.lastOrNull()?.let { Text(Chart.formatTick(it) + series.unit, color = t.text, fontFamily = t.mono, fontSize = 13.sp) }
        }
        if (!SeriesState(series, kind)) SparkCanvas(series, tone, (node.props["trendline"] as? JsonPrimitive)?.content == "true")
        val dropped = Chart.countDropped(series.points, listOf("v"))
        if (dropped > 0) Text("$dropped reading${if (dropped == 1) "" else "s"} missing", color = t.muted, fontSize = 11.sp)
        series.downsampledFrom?.let { Text("downsampled from $it", color = t.muted, fontSize = 11.sp) }
    }
}

@Composable
fun LineChartView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val series = seriesOf(host, node)
    val specs = ((node.props["series"] as? JsonArray) ?: JsonArray(emptyList())).mapNotNull { it as? JsonObject }.filter { it.str("key") != null }.take(4)
    val all = specs.flatMap { s -> Chart.seriesValues(series.points, s.str("key")!!).map { it.v } }
    val kind = Chart.classify(all)
    val domain = Chart.yDomain(all, zeroBase = false)
    val w = 320.0; val h = 160.0; val padL = 34.0; val padR = 8.0; val padT = if (node.props.str("yLabel") != null) 20.0 else 8.0; val padB = 22.0
    val px: (Int) -> Double = { i -> val usable = w - padL - padR; if (series.points.size <= 1) padL + usable / 2 else padL + usable * i / (series.points.size - 1) }
    val py: (Double) -> Double = { v -> val usable = h - padT - padB; val span = (domain.max - domain.min).let { if (it == 0.0) 1.0 else it }; padT + usable * (1 - (v - domain.min) / span) }
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(Functions.toDisplayString(node.props["label"]), color = t.muted, fontSize = 12.sp)
        if (!SeriesState(series, kind)) Canvas(modifier = Modifier.fillMaxWidth().height(160.dp).semantics { contentDescription = "linechart" }) {
            val sx = size.width / w.toFloat(); val sy = size.height / h.toFloat()
            for (tick in Chart.ticks(domain, 3)) { val yy = py(tick).toFloat() * sy; drawLine(t.chartGrid, Offset(padL.toFloat() * sx, yy), Offset(size.width - padR.toFloat() * sx, yy), 1f) }
            specs.forEachIndexed { si, s ->
                val tone = Chart.normalizeTone(s["tone"])
                val color = if (tone != Tone.NEUTRAL) t.tone(tone) else t.token("series-${Chart.seriesSlot(si)}")
                drawSegments(Chart.lineSegments(Chart.seriesValues(series.points, s.str("key")!!), px, py), color, 2f * sx, sx, sy)
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            specs.forEachIndexed { si, s -> val tone = Chart.normalizeTone(s["tone"]); val c = if (tone != Tone.NEUTRAL) t.tone(tone) else t.token("series-${Chart.seriesSlot(si)}")
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) { Box(Modifier.width(10.dp).height(3.dp).background(c)); Text(s.str("label") ?: s.str("key")!!, color = t.muted, fontSize = 11.sp) } }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) { for (tick in Chart.ticks(domain, 3).reversed()) Text(Chart.formatTick(tick) + series.unit, color = t.chartAxis, fontSize = 10.sp, fontFamily = t.mono) }
    }
}

@Composable
fun BarChartView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val series = seriesOf(host, node)
    val bars = Chart.seriesValues(series.points).map { p -> Functions.toDisplayString(series.points[p.i]["t"]) to p.v }
    val kind = Chart.classify(bars.map { it.second })
    val domain = Chart.yDomain(bars.map { it.second }, zeroBase = true)   // F094: zero-based is normative for bars
    val maxAbs = maxOf(Math.abs(domain.min), Math.abs(domain.max)).let { if (it == 0.0) 1.0 else it }
    val tone = Chart.normalizeTone(node.props["tone"])
    val horizontal = node.props.str("orientation") == "horizontal"
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(Functions.toDisplayString(node.props["label"]), color = t.muted, fontSize = 12.sp)
        if (!SeriesState(series, kind)) {
            if (horizontal) Column(verticalArrangement = Arrangement.spacedBy(4.dp)) { for ((cat, v) in bars) Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(cat, color = t.muted, fontSize = 11.sp, modifier = Modifier.width(72.dp)); Box(Modifier.weight(1f).height(10.dp)) { Box(Modifier.fillMaxWidth((Math.abs(v) / maxAbs).toFloat().coerceIn(0f, 1f)).height(10.dp).background(t.tone(tone), RoundedCornerShape(2.dp))) }
                Text(Chart.formatTick(v), color = t.text, fontFamily = t.mono, fontSize = 11.sp) } }
            else Row(Modifier.fillMaxWidth().height(120.dp), horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.Bottom) { for ((cat, v) in bars) Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                Box(Modifier.fillMaxWidth().height((100 * Math.abs(v) / maxAbs).toFloat().coerceIn(0f, 100f).dp).background(t.tone(tone), RoundedCornerShape(2.dp))); Text(cat.takeLast(5), color = t.chartAxis, fontSize = 9.sp, fontFamily = t.mono) } }
        }
    }
}
