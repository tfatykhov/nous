package us.fatykhov.nous.companion

import android.app.Application
import us.fatykhov.nous.companion.data.AppGraph

class NousApp : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)
        // Firebase is initialised here, before any service can run (spec §6.2) —
        // only when valid cached options exist; otherwise push stays off.
        graph.push.initialiseIfConfigured()
        graph.lifecycle.install()
    }
}
