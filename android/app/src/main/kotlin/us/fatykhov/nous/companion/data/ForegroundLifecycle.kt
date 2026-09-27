package us.fatykhov.nous.companion.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import us.fatykhov.nous.companion.core.Connection

/**
 * Spec §7.3: the SSE stream runs only while the app is in the foreground.
 * ON_START marks the store RESYNCING (R17: never present old state as
 * current) and connects; ON_STOP disconnects with no extra grace (the
 * cached-apps freezer would race a longer timer). A network switch cancels
 * the stream so R2 reconnects immediately.
 */
class ForegroundLifecycle(private val graph: AppGraph) : DefaultLifecycleObserver {
    fun install() {
        ProcessLifecycleOwner.get().lifecycle.addObserver(this)
        val cm = graph.appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        cm?.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onLost(network: Network) { graph.http.cancelStream() }
            override fun onAvailable(network: Network) { graph.http.cancelStream() }
        })
    }

    override fun onStart(owner: LifecycleOwner) {
        if (!graph.settings.configured) return
        graph.store.connection = Connection.RESYNCING
        graph.engine.connect()
        graph.push.registerIfPossible()
    }

    override fun onStop(owner: LifecycleOwner) {
        graph.engine.stop()
        graph.http.cancelStream()
    }
}
