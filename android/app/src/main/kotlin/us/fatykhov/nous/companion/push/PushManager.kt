package us.fatykhov.nous.companion.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationManagerCompat
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import us.fatykhov.nous.companion.core.Http
import us.fatykhov.nous.companion.data.Settings

/**
 * Push plumbing (spec §6.2, §6.6). Everything here is a no-op until the
 * server serves valid Firebase options AND the user allowed notifications;
 * the app works foreground-only without push.
 */
class PushManager(private val context: Context, private val settings: Settings, private val http: Http) {
    companion object {
        const val CHANNEL_APPROVALS = "approvals_v1"
        const val CHANNEL_UPDATES = "updates_v1"
        const val CHANNEL_GENERAL = "general_v1"
        const val NOTIF_ID = 1
        fun channelFor(priority: Int) = when (priority) { 2 -> CHANNEL_APPROVALS; 1 -> CHANNEL_UPDATES; else -> CHANNEL_GENERAL }
    }

    @Volatile var status: String = "not configured"; private set

    fun initialiseIfConfigured() {
        ensureChannels()
        val o = settings.firebaseOptions ?: run { status = "push not configured"; return }
        if (FirebaseApp.getApps(context).isNotEmpty()) return
        try {
            FirebaseApp.initializeApp(
                context,
                FirebaseOptions.Builder()
                    .setProjectId(o["project_id"]).setApplicationId(o["application_id"]!!)
                    .setApiKey(o["api_key"]!!).setGcmSenderId(o["sender_id"]).build(),
            )
            status = "firebase ready"
        } catch (e: Exception) { status = "firebase init failed: ${e.message}" }
    }

    /** Fetch `/a2ui/push/config` (404 == disabled) and cache validated options; then register. */
    suspend fun refreshConfig() {
        val res = try { http.get("/a2ui/push/config") } catch (_: Exception) { status = "server unreachable"; return }
        if (res.status == 404) { status = "push unavailable (server has no push)"; return }
        if (!res.ok) { status = "push config HTTP ${res.status}"; return }
        val o = runCatching { Json.parseToJsonElement(res.body).jsonObject }.getOrNull() ?: return
        if (o["enabled"]?.jsonPrimitive?.content != "true") { status = "push disabled: ${o["reason"]?.jsonPrimitive?.content ?: "server"}"; return }
        val opts = listOf("project_id", "application_id", "api_key", "sender_id").associateWith { o[it]?.jsonPrimitive?.content ?: "" }
        if (!Settings.validFirebaseOptions(opts)) { status = "push config invalid"; return }
        val changed = settings.firebaseOptions != opts
        settings.firebaseOptions = opts
        if (changed && FirebaseApp.getApps(context).isNotEmpty()) { status = "firebase project changed — restart the app"; return }
        initialiseIfConfigured()
        registerIfPossible()
    }

    fun registerIfPossible() {
        if (settings.firebaseOptions == null || !settings.configured) return
        WorkManager.getInstance(context).enqueueUniqueWork(
            "push-token", ExistingWorkPolicy.REPLACE, OneTimeWorkRequestBuilder<TokenWorker>().build(),
        )
    }

    fun notificationsEnabled(): Boolean {
        val nm = NotificationManagerCompat.from(context)
        if (!nm.areNotificationsEnabled()) return false
        val ch = nm.getNotificationChannel(CHANNEL_UPDATES)
        return ch == null || ch.importance != NotificationManager.IMPORTANCE_NONE
    }

    private fun ensureChannels() {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(CHANNEL_APPROVALS, "Approvals", NotificationManager.IMPORTANCE_HIGH).apply { description = "Cards that need you" })
        nm.createNotificationChannel(NotificationChannel(CHANNEL_UPDATES, "Updates", NotificationManager.IMPORTANCE_DEFAULT).apply { description = "Findings, failed runs, sweeps" })
        nm.createNotificationChannel(NotificationChannel(CHANNEL_GENERAL, "General", NotificationManager.IMPORTANCE_LOW).apply { description = "Everything else" })
    }
}
