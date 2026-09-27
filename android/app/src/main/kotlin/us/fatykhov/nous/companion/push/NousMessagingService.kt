package us.fatykhov.nous.companion.push

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import us.fatykhov.nous.companion.MainActivity
import us.fatykhov.nous.companion.NousApp
import us.fatykhov.nous.companion.R

/**
 * Spec §6.6. Data-only messages: `surface` (post, unless tombstoned or the
 * app is in the foreground), `dismiss` (cancel by tag + tombstone), `test`.
 * The SSE `deleteSurface` event NEVER cancels a notification; only a
 * dismiss or opening the surface does.
 */
class NousMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) { (application as NousApp).graph.push.registerIfPossible() }

    override fun onMessageReceived(message: RemoteMessage) {
        val d = message.data
        if (d["v"] != "1") return
        val graph = (application as NousApp).graph
        when (d["type"]) {
            "surface" -> {
                val id = d["surface_id"] ?: return
                if (Tombstones.contains(this, id)) return
                if (ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return
                post(this, id, d["title"] ?: "Nous", d["body"] ?: "", d["priority"]?.toIntOrNull() ?: 0, d["expires_at"])
            }
            "dismiss" -> {
                val id = d["surface_id"] ?: return
                Notifications.cancel(this, id)
                Tombstones.add(this, id)
            }
            "test" -> post(this, "test", d["title"] ?: "Nous", d["body"] ?: "Test notification", 1, null)
        }
        graph.push.registerIfPossible()
    }

    override fun onDeletedMessages() { ReconcileFlag.pending = true }

    private fun post(ctx: Context, surfaceId: String, title: String, body: String, priority: Int, expiresAt: String?) {
        val intent = Intent(ctx, MainActivity::class.java).apply {
            // The data URI only makes each PendingIntent distinct; there is no exported scheme.
            data = Uri.parse("nouscompanion://s/${Uri.encode(surfaceId)}")
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pi = PendingIntent.getActivity(ctx, surfaceId.hashCode(), intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, PushManager.channelFor(priority))
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title).setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(pi).setAutoCancel(true)
            .apply {
                expiresAt?.let { runCatching { java.time.OffsetDateTime.parse(it).toInstant().toEpochMilli() - System.currentTimeMillis() }.getOrNull()?.takeIf { ms -> ms > 0 }?.let { ms -> setTimeoutAfter(ms) } }
            }
            .build()
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(surfaceId, PushManager.NOTIF_ID, n)
    }
}

object Notifications {
    fun cancel(ctx: Context, surfaceId: String) {
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(surfaceId, PushManager.NOTIF_ID)
    }
    /** Spec §6.6 reconcile: cancel every active notification whose tag is not live. */
    fun reconcile(ctx: Context, liveIds: Set<String>) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        for (sbn in nm.activeNotifications) {
            val tag = sbn.tag ?: continue
            if (sbn.id == PushManager.NOTIF_ID && tag != "test" && tag !in liveIds) nm.cancel(tag, PushManager.NOTIF_ID)
        }
    }
}

/** 48 h / 500-id tombstone set so a late `surface` message never resurrects a dismissed card. */
object Tombstones {
    private const val FILE = "tombstones"
    private const val TTL = 48L * 3600 * 1000
    private const val MAX = 500
    fun add(ctx: Context, id: String) {
        val p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val e = p.edit().putLong(id, now)
        val all = p.all.entries.filter { (it.value as? Long ?: 0L) > now - TTL }.sortedBy { it.value as Long }
        if (all.size >= MAX) all.take(all.size - MAX + 1).forEach { e.remove(it.key) }
        p.all.keys.filter { (p.getLong(it, 0L)) <= now - TTL }.forEach { e.remove(it) }
        e.apply()
    }
    fun contains(ctx: Context, id: String): Boolean {
        val t = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getLong(id, 0L)
        return t > System.currentTimeMillis() - TTL
    }
}

object ReconcileFlag { @Volatile var pending = false }
