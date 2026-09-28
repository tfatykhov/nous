package us.fatykhov.nous.companion.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily

/**
 * The six companion themes as Compose token sets (spec §8.4), values copied
 * from `dashboard-app/src/companion/companion.css`. Applied PER SURFACE
 * through [LocalNousTheme], mirroring `data-theme` on each surface root.
 */
data class NousTheme(
    val id: String,
    val bg: Color, val surface: Color, val surfaceHover: Color, val border: Color,
    val accent: Color, val accentDim: Color, val accentGlow: Color,
    val text: Color, val muted: Color,
    val ok: Color, val warn: Color, val crit: Color, val locked: Color, val soft: Color,
    val onAccent: Color, val scrim: Color, val nodeDecision: Color,
    val series1: Color, val series2: Color, val series3: Color, val series4: Color,
    val chartAxis: Color, val chartGrid: Color,
    val display: FontFamily = FontFamily.SansSerif,
    val mono: FontFamily = FontFamily.Monospace,
    val light: Boolean = false,
) {
    /** Semantic token by name, for `:core` results like `Graphs.dagStatusToken`. */
    fun token(name: String): Color = when (name) {
        "ok" -> ok; "warn" -> warn; "crit" -> crit; "accent" -> accent; "muted" -> muted
        "locked" -> locked; "soft" -> soft; "node-decision" -> nodeDecision; "border" -> border
        "series-1" -> series1; "series-2" -> series2; "series-3" -> series3; "series-4" -> series4
        "chart-axis" -> chartAxis; "chart-grid" -> chartGrid
        else -> muted
    }
    fun tone(t: us.fatykhov.nous.companion.core.Tone): Color = when (t) {
        us.fatykhov.nous.companion.core.Tone.OK -> ok
        us.fatykhov.nous.companion.core.Tone.WARN -> warn
        us.fatykhov.nous.companion.core.Tone.CRIT -> crit
        us.fatykhov.nous.companion.core.Tone.NEUTRAL -> chartAxis
    }
    fun toneInk(t: us.fatykhov.nous.companion.core.Tone): Color = if (t == us.fatykhov.nous.companion.core.Tone.NEUTRAL) soft else tone(t)
}

private fun c(hex: Long) = Color(hex or 0xFF000000)
private fun rgba(r: Int, g: Int, b: Int, a: Float) = Color(r, g, b, (a * 255).toInt())

object Themes {
    val nousDefault = NousTheme(
        id = "nous-default",
        bg = c(0x0a0a0f), surface = c(0x12121a), surfaceHover = c(0x181824), border = c(0x1e1e2e),
        accent = c(0x7c6af7), accentDim = c(0x4f46a8), accentGlow = rgba(124, 106, 247, 0.15f),
        text = c(0xe2e2f0), muted = c(0x6b6b8a),
        ok = c(0x34d399), warn = c(0xfbbf24), crit = c(0xf87171), locked = c(0x5fb3a1), soft = c(0x8a8fa3),
        onAccent = c(0xffffff), scrim = rgba(10, 10, 15, 0.72f), nodeDecision = c(0xa78bfa),
        series1 = c(0x7c6af7), series2 = c(0x34d399), series3 = c(0xfbbf24), series4 = c(0x60a5fa),
        chartAxis = c(0x6b6b8a), chartGrid = c(0x1e1e2e),
    )
    val alpineDusk = NousTheme(
        id = "alpine-dusk",
        bg = c(0x0e1014), surface = c(0x161a20), surfaceHover = c(0x1e232b), border = c(0x232a33),
        accent = c(0xe8833a), accentDim = c(0xa85a25), accentGlow = rgba(232, 131, 58, 0.15f),
        text = c(0xece6dd), muted = c(0x8a8072),
        ok = c(0x5fb3a1), warn = c(0xe8a33a), crit = c(0xd9553f), locked = c(0x5fb3a1), soft = c(0x8a8072),
        onAccent = c(0x1a1206), scrim = rgba(14, 16, 20, 0.74f), nodeDecision = c(0xc79bde),
        series1 = c(0xe8833a), series2 = c(0x5fb3a1), series3 = c(0xe8a33a), series4 = c(0x8fb8d9),
        chartAxis = c(0x8a8072), chartGrid = c(0x232a33), display = FontFamily.Serif,
    )
    val harbor = NousTheme(
        id = "harbor",
        bg = c(0x0b1016), surface = c(0x121a24), surfaceHover = c(0x182230), border = c(0x1f2c3a),
        accent = c(0x4a90d9), accentDim = c(0x2f5f92), accentGlow = rgba(74, 144, 217, 0.15f),
        text = c(0xdbe6f0), muted = c(0x7089a0),
        ok = c(0x3fb6a0), warn = c(0xe0a94a), crit = c(0xe0685f), locked = c(0x4a90d9), soft = c(0x7089a0),
        onAccent = c(0x06101a), scrim = rgba(11, 16, 22, 0.74f), nodeDecision = c(0x8fb0e0),
        series1 = c(0x4a90d9), series2 = c(0x3fb6a0), series3 = c(0xe0a94a), series4 = c(0xa98fd9),
        chartAxis = c(0x7089a0), chartGrid = c(0x1f2c3a),
    )
    val paper = NousTheme(
        id = "paper",
        bg = c(0xf4f1ea), surface = c(0xfbf9f4), surfaceHover = c(0xf0ece1), border = c(0xddd6c8),
        accent = c(0xb5651d), accentDim = c(0x8a4d16), accentGlow = rgba(181, 101, 29, 0.12f),
        text = c(0x2a2622), muted = c(0x6e675b),
        ok = c(0x2f8f6b), warn = c(0xb5811d), crit = c(0xc0432f), locked = c(0x2f8f6b), soft = c(0x6e675b),
        onAccent = c(0xfbf9f4), scrim = rgba(42, 38, 34, 0.42f), nodeDecision = c(0x7a5aa8),
        series1 = c(0xb5651d), series2 = c(0x2f8f6b), series3 = c(0xb5811d), series4 = c(0x3a6ea8),
        chartAxis = c(0x6e675b), chartGrid = c(0xddd6c8), display = FontFamily.Serif, light = true,
    )
    val signal = NousTheme(
        id = "signal",
        bg = c(0x08080a), surface = c(0x101014), surfaceHover = c(0x17171d), border = c(0x24242e),
        accent = c(0x00d9a3), accentDim = c(0x009873), accentGlow = rgba(0, 217, 163, 0.16f),
        text = c(0xf4f4fa), muted = c(0x7a7a92),
        ok = c(0x00d9a3), warn = c(0xffcc33), crit = c(0xff5c5c), locked = c(0x00d9a3), soft = c(0x7a7a92),
        onAccent = c(0x04120d), scrim = rgba(8, 8, 10, 0.8f), nodeDecision = c(0xb18cff),
        series1 = c(0x00d9a3), series2 = c(0x60a5fa), series3 = c(0xffcc33), series4 = c(0xff5c5c),
        chartAxis = c(0x7a7a92), chartGrid = c(0x24242e), display = FontFamily.Monospace,
    )
    val report = NousTheme(
        id = "report",
        bg = c(0x0a0c11), surface = c(0x131822), surfaceHover = c(0x182030), border = c(0x212b3b),
        accent = c(0x7c9cd0), accentDim = c(0x4f6fa0), accentGlow = rgba(124, 156, 208, 0.15f),
        text = c(0xe9edf5), muted = c(0x8d99ae),
        ok = c(0x4ade80), warn = c(0xfbbf24), crit = c(0xfb7185), locked = c(0x94a3b8), soft = c(0x94a3b8),
        onAccent = c(0x0a0c11), scrim = rgba(10, 12, 17, 0.74f), nodeDecision = c(0xc4b5fd),
        series1 = c(0xf0abfc), series2 = c(0x4ade80), series3 = c(0xfbbf24), series4 = c(0x60a5fa),
        chartAxis = c(0x8d99ae), chartGrid = c(0x212b3b), display = FontFamily.Serif,
    )
    val all = listOf(nousDefault, alpineDusk, harbor, paper, signal, report)
    /** R14: an unknown or empty theme id renders nous-default. */
    fun byId(id: String?): NousTheme = all.firstOrNull { it.id == id } ?: nousDefault
}

val LocalNousTheme = staticCompositionLocalOf { Themes.nousDefault }

/**
 * Installs the theme AND the press feedback that goes with it. Without an
 * indication of its own, every `Modifier.clickable` in the app — inbox cards,
 * tabs, choice chips — fell back to Compose's debug indication: a faint BLACK
 * overlay, which on a dark surface is no feedback at all. The ripple is drawn
 * in the theme's text colour, so it is light on the dark themes and dark on
 * `paper`, at an alpha strong enough to be seen on either.
 */
@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun NousThemed(theme: NousTheme, content: @Composable () -> Unit) {
    CompositionLocalProvider(
        LocalNousTheme provides theme,
        androidx.compose.foundation.LocalIndication provides androidx.compose.material3.ripple(color = theme.text),
        androidx.compose.material3.LocalRippleConfiguration provides androidx.compose.material3.RippleConfiguration(
            color = theme.text,
            rippleAlpha = androidx.compose.material.ripple.RippleAlpha(draggedAlpha = 0.18f, focusedAlpha = 0.14f, hoveredAlpha = 0.10f, pressedAlpha = 0.22f),
        ),
        content = content,
    )
}
