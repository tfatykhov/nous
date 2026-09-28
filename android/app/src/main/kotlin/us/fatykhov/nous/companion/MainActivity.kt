package us.fatykhov.nous.companion

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import us.fatykhov.nous.companion.push.Notifications
import us.fatykhov.nous.companion.ui.CompanionApp
import us.fatykhov.nous.companion.ui.Route

class MainActivity : ComponentActivity() {
    private var route by mutableStateOf<Route>(Route.Inbox)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        route = routeFor(intent)
        setContent { CompanionApp((application as NousApp).graph, route) { route = it } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        route = routeFor(intent)
    }

    /** A notification tap carries `nouscompanion://s/<id>` (only to make PendingIntents distinct). */
    private fun routeFor(intent: Intent?): Route {
        val data = intent?.data ?: return route
        if (data.scheme == "nouscompanion" && data.host == "s") {
            val id = data.pathSegments.firstOrNull() ?: return Route.Inbox
            Notifications.cancel(this, id)
            return Route.Surface(id)
        }
        return route
    }
}
