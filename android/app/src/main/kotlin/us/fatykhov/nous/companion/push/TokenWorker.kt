package us.fatykhov.nous.companion.push

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import us.fatykhov.nous.companion.NousApp

/** Spec §6.6: registers the FCM token with `PUT /a2ui/push/tokens`; a no-op without valid options. */
class TokenWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val graph = (applicationContext as NousApp).graph
        if (graph.settings.firebaseOptions == null) return Result.success()
        val token = try { FirebaseMessaging.getInstance().token.await() } catch (e: Exception) {
            TokenState.lastError = "token: ${e.message}"; return Result.retry()
        }
        val body = buildJsonObject {
            put("installation_id", graph.settings.installationId)
            put("fcm_token", token)
            put("name", graph.settings.deviceName)
            put("app_version", runCatching { applicationContext.packageManager.getPackageInfo(applicationContext.packageName, 0).versionName ?: "" }.getOrDefault(""))
            put("notifications_enabled", graph.push.notificationsEnabled())
        }
        // PUT, not POST: the route is registered `methods=["PUT"]` (spec §6.3).
        // A POST returned 405, which the 405-tolerant branch below used to
        // treat as "server has no push" — so registration would have failed
        // silently, forever, while reporting success.
        val res = try { graph.http.putJson("/a2ui/push/tokens", body.toString()) } catch (e: Exception) {
            TokenState.lastError = "register: ${e.message}"; return Result.retry()
        }
        // 404 means a server older than F097 — a state, not a failure. 405
        // would mean the route exists and we used the wrong verb, which is a
        // bug and must stay visible.
        return if (res.ok || res.status == 404) { TokenState.lastError = null; TokenState.registeredAt = System.currentTimeMillis(); Result.success() }
        else { TokenState.lastError = "register: HTTP ${res.status}"; Result.retry() }
    }
}

/** Diagnostics for Settings (spec §7.2). */
object TokenState {
    @Volatile var registeredAt: Long? = null
    @Volatile var lastError: String? = null
}
