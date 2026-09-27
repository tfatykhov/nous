package us.fatykhov.nous.companion.data

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import us.fatykhov.nous.companion.push.PushManager
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.SurfaceStore
import us.fatykhov.nous.companion.core.SyncEngine

/** Hand-wired object graph (no DI framework for a beta). One per process. */
class AppGraph(context: Context) {
    val appContext: Context = context.applicationContext
    val settings = Settings(appContext)
    val store = SurfaceStore()
    val mainScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val http = OkHttpTransport(settings)
    val engine = SyncEngine(store, http, mainScope)
    val functions = Functions(fmt = AndroidFormatter, opener = { url -> UrlOpener.open(appContext, url) })
    val push = PushManager(appContext, settings, http)
    val lifecycle = ForegroundLifecycle(this)
}
