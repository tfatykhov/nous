package us.fatykhov.nous.companion.ui

import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import us.fatykhov.nous.companion.core.Child
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.core.Scope
import us.fatykhov.nous.companion.core.Walker
import us.fatykhov.nous.companion.core.stringOrNull
import us.fatykhov.nous.companion.data.UrlOpener
import us.fatykhov.nous.companion.ui.catalog.Registry as UiRegistry

/** Semantic tags the fixture tests assert on (spec §10.4). */
object Tags {
    const val FALLBACK = "nous-fallback"
    const val PLACEHOLDER = "nous-placeholder"
}

/** A renderer: draws one component given its props and scope. */
typealias RendererFn = @Composable (node: Node.Render) -> Unit

/** The walker's decisions (`:core`) drawn by Compose (spec §3.2 R11, R12). */
@Composable
fun Render(componentId: String, scope: Scope?, depth: Int, ancestors: List<String>) {
    val host = LocalSurfaceHost.current
    val walker = Walker(UiRegistry.names)
    when (val n = walker.resolve(host.surface, componentId, scope, depth, ancestors)) {
        is Node.Placeholder -> Text("⟳", modifier = Modifier.semantics { contentDescription = "${Tags.PLACEHOLDER}:${n.reason} at ${n.componentId}" }, color = LocalNousTheme.current.muted)
        is Node.Dangling -> Text("…", modifier = Modifier.semantics { contentDescription = "${Tags.PLACEHOLDER}:waiting for ${n.componentId}" }, color = LocalNousTheme.current.muted)
        is Node.Unknown -> FallbackCard(n.component)
        is Node.Render -> UiRegistry.renderers[n.component]!!.invoke(n)
    }
}

/** Spec §8.2: never blank, never a throw — a card that names the component and opens the web. */
@Composable
fun FallbackCard(component: String) {
    val t = LocalNousTheme.current
    val host = LocalSurfaceHost.current
    Column(
        modifier = Modifier.fillMaxWidth()
            .border(1.dp, t.border, RoundedCornerShape(8.dp)).padding(12.dp)
            .semantics { contentDescription = "${Tags.FALLBACK}:$component" },
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Text(buildString { append(component); append(" isn't supported in the Android beta yet.") }, color = t.text, fontSize = 14.sp)
        TextButton(onClick = { UrlOpener.open(host.graph.appContext, host.graph.settings.baseUrl + "/companion/a/" + java.net.URLEncoder.encode(host.surfaceId, "UTF-8")) }) {
            Text("Open in web", color = t.accent, fontWeight = FontWeight.SemiBold)
        }
    }
}

/** A `children` prop through the walker's expansion (R12), each slot rendered in turn. */
@Composable
fun Children(children: JsonElement?, node: Node.Render) {
    val host = LocalSurfaceHost.current
    val s = host.surface ?: return
    val walker = Walker(UiRegistry.names)
    // Keyed by component identity, like the web's keyed each: an
    // `updateComponents` that inserts or reorders children would otherwise
    // hand a sibling's remembered state (a Modal's open, a Tabs' selection,
    // a Button's busy) to whichever component now sits at that position.
    for (c in walker.children(s, children, node.scope)) when (c) {
        is Child.Static -> androidx.compose.runtime.key(c.componentId) { Render(c.componentId, node.scope, node.depth, node.ancestors) }
        is Child.Template -> androidx.compose.runtime.key(c.componentId, c.scope) { Render(c.componentId, c.scope, node.depth, node.ancestors) }
        is Child.Omitted -> Text(c.note, color = LocalNousTheme.current.muted, fontSize = 12.sp)
    }
}

/** A single `child` id prop. */
@Composable
fun ChildSlot(child: JsonElement?, node: Node.Render) {
    val id = child?.stringOrNull ?: return
    Render(id, node.scope, node.depth, node.ancestors)
}

internal fun JsonObject.str(key: String): String? = this[key]?.stringOrNull
internal fun JsonObject.arr(key: String): JsonArray? = this[key] as? JsonArray
