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
        val res = try { graph.http.postJson("/a2ui/push/tokens", body.toString()) } catch (e: Exception) {
            TokenState.lastError = "register: ${e.message}"; return Result.retry()
        }
        // The server may not have push yet (PR 1 pending): 404/405 is "not available", not a failure to retry.
        return if (res.ok || res.status == 404 || res.status == 405) { TokenState.lastError = null; TokenState.registeredAt = System.currentTimeMillis(); Result.success() }
        else { TokenState.lastError = "register: HTTP ${res.status}"; Result.retry() }
    }
}

/** Diagnostics for Settings (spec §7.2). */
object TokenState {
    @Volatile var registeredAt: Long? = null
    @Volatile var lastError: String? = null
}
