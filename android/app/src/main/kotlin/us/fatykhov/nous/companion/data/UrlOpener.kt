package us.fatykhov.nous.companion.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import us.fatykhov.nous.companion.core.Functions

/** `openUrl`: the same scheme allowlist as the web (R13), then the system browser. */
object UrlOpener {
    fun open(context: Context, url: String) {
        if (!Functions.isSafeUrl(url)) return
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url.trim())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: Exception) {
            // No handler: the tap does nothing, which is what the web's failed window.open does.
        }
    }
}
