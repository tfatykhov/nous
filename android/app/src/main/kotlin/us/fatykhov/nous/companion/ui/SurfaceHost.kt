package us.fatykhov.nous.companion.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.DisposableEffect
import kotlinx.serialization.json.JsonElement
import us.fatykhov.nous.companion.core.EvalContext
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.Scope
import us.fatykhov.nous.companion.core.SurfaceState
import us.fatykhov.nous.companion.core.SurfaceStore
import us.fatykhov.nous.companion.core.SyncEngine
import us.fatykhov.nous.companion.data.AppGraph

/** Everything a renderer needs, provided once per surface. */
class SurfaceHost(val graph: AppGraph, val surfaceId: String) {
    val store: SurfaceStore get() = graph.store
    val engine: SyncEngine get() = graph.engine
    val functions: Functions get() = graph.functions
    val surface: SurfaceState? get() = store.surfaces[surfaceId]
    fun ctx(scope: Scope?) = EvalContext(surface?.dataModel ?: kotlinx.serialization.json.JsonObject(emptyMap()), scope)
    fun resolve(v: JsonElement?, scope: Scope?): JsonElement? = try { functions.resolveDynamic(v, ctx(scope)) } catch (_: Exception) { null }
    fun text(v: JsonElement?, scope: Scope?): String = Functions.toDisplayString(resolve(v, scope))
}

val LocalSurfaceHost = compositionLocalOf<SurfaceHost> { error("no SurfaceHost") }

/**
 * `:core.SurfaceStore` is not observable by Compose; a version counter
 * bumped from `onChange` is what recomposes the tree. One listener per
 * process, installed lazily.
 */
@Composable
fun rememberStoreVersion(store: SurfaceStore): State<Int> {
    val v = remember(store) { mutableIntStateOf(0) }
    DisposableEffect(store) {
        val l = { v.intValue = v.intValue + 1 }
        store.onChange(l)
        onDispose { store.removeListener(l) }
    }
    return v
}
