package us.fatykhov.nous.companion.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import kotlinx.coroutines.launch
import us.fatykhov.nous.companion.core.Connection
import us.fatykhov.nous.companion.push.Notifications
import us.fatykhov.nous.companion.push.ReconcileFlag

/**
 * Spec §7.3: the SSE stream runs only while the app is in the foreground.
 * ON_START marks the store RESYNCING (R17: never present old state as
 * current) and connects; ON_STOP disconnects with no extra grace (the
 * cached-apps freezer would race a longer timer). A network switch cancels
 * the stream so R2 reconnects immediately.
 */
class ForegroundLifecycle(private val graph: AppGraph) : DefaultLifecycleObserver {
    private var lastConnection: Connection? = null

    fun install() {
        ProcessLifecycleOwner.get().lifecycle.addObserver(this)
        val cm = graph.appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        cm?.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onLost(network: Network) { graph.http.cancelStream() }
            override fun onAvailable(network: Network) { graph.http.cancelStream() }
        })
        // Spec §6.6 reconcile runs on EVERY completed hydration, from the store
        // — not from a screen. It used to live in the Inbox composable, so a
        // cold start from a notification (which opens the Surface route) or a
        // foreground on Settings never reconciled, and `onDeletedMessages`'s
        // pending flag had no consumer until the user happened to visit the
        // inbox (codex P2).
        graph.store.onChange {
            val now = graph.store.connection
            val becameLive = now == Connection.LIVE && lastConnection != Connection.LIVE
            lastConnection = now
            if (now == Connection.LIVE && (becameLive || ReconcileFlag.pending)) {
                Notifications.reconcile(graph.appContext, graph.store.surfaces.keys.toSet())
                ReconcileFlag.pending = false
            }
        }
    }

    override fun onStart(owner: LifecycleOwner) {
        if (!graph.settings.configured) return
        graph.store.connection = Connection.RESYNCING
        graph.engine.connect()
        // A config fetch that failed at first run (transient outage) left no
        // Firebase options, and registerIfPossible() is a no-op without them —
        // push would stay off for the life of the install. Fetch again when
        // missing; otherwise just re-register (codex P2).
        if (graph.settings.firebaseOptions == null) graph.mainScope.launch { graph.push.refreshConfig() }
        else graph.push.registerIfPossible()
    }

    override fun onStop(owner: LifecycleOwner) {
        graph.engine.stop()
        graph.http.cancelStream()
    }
}
