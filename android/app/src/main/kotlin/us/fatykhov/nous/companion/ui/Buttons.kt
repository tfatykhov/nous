package us.fatykhov.nous.companion.ui

import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

/**
 * Every button in the app, so they all look and respond like one.
 *
 * The first build's buttons read as static labels: the default variant was an
 * outline with a TRANSPARENT fill (the web fills it with `--surface-hover`),
 * and a press produced nothing visible — the app installed no ripple, so
 * Compose fell back to its debug indication, a faint black overlay that is
 * invisible on a dark surface. A button now has a fill at rest, and a press
 * answers three ways: a ripple, the surface easing down to 96 %, and the
 * border / fill shifting the way the web's `:hover` does.
 */
enum class ButtonKind { Default, Primary, Borderless }

/**
 * The ink a button asks its label to use. The catalog `Text` renderer paints
 * in the theme's text colour, so a primary button's label ignored the
 * button's `on-accent` ink; on the web the child simply inherits it. Null
 * outside a button.
 */
val LocalButtonInk = compositionLocalOf<Color?> { null }

const val PRESSED_SCALE = 0.96f

@Composable
fun NousButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    kind: ButtonKind = ButtonKind.Default,
    busy: Boolean = false,
    shape: Shape = RoundedCornerShape(8.dp),
    contentPadding: PaddingValues = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
    content: @Composable RowScope.() -> Unit,
) {
    val t = LocalNousTheme.current
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val active = pressed && enabled
    val scale by animateFloatAsState(
        targetValue = if (active) PRESSED_SCALE else 1f,
        animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy, stiffness = Spring.StiffnessMedium),
        label = "press-scale",
    )
    val container by animateColorAsState(
        when (kind) {
            ButtonKind.Primary -> if (active) t.accentDim else t.accent
            ButtonKind.Default -> if (active) t.border else t.surfaceHover
            ButtonKind.Borderless -> if (active) t.surfaceHover else Color.Transparent
        },
        label = "press-fill",
    )
    val ink = when (kind) {
        ButtonKind.Primary -> t.onAccent
        ButtonKind.Default -> t.text
        ButtonKind.Borderless -> if (active) t.text else t.soft
    }
    val border = if (kind == ButtonKind.Default) BorderStroke(1.dp, if (active) t.accent else t.border) else null
    Button(
        onClick = onClick,
        modifier = modifier.graphicsLayer { scaleX = scale; scaleY = scale },
        enabled = enabled && !busy,
        shape = shape,
        colors = ButtonDefaults.buttonColors(
            containerColor = container, contentColor = ink,
            // The web's `:disabled { opacity: 0.5 }`: visibly the same button, visibly unavailable.
            disabledContainerColor = container.copy(alpha = container.alpha * 0.5f),
            disabledContentColor = ink.copy(alpha = 0.5f),
        ),
        border = border,
        elevation = if (kind == ButtonKind.Borderless) null else ButtonDefaults.buttonElevation(defaultElevation = 2.dp, pressedElevation = 0.dp, disabledElevation = 0.dp),
        interactionSource = interaction,
        contentPadding = contentPadding,
    ) {
        CompositionLocalProvider(LocalButtonInk provides if (enabled && !busy) ink else ink.copy(alpha = 0.5f)) {
            if (busy) {
                CircularProgressIndicator(
                    modifier = Modifier.size(14.dp).semantics { contentDescription = "busy" },
                    color = ink, strokeWidth = 2.dp,
                )
                Spacer(Modifier.width(8.dp))
            }
            content()
        }
    }
}
