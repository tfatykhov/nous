package us.fatykhov.nous.companion.push

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import us.fatykhov.nous.companion.NousApp
import us.fatykhov.nous.companion.core.Refusal

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
        val (outcome, error) = registrationOutcome(res.status, res.body)
        TokenState.lastError = error
        return when (outcome) {
            Registration.DONE -> { TokenState.registeredAt = System.currentTimeMillis(); Result.success() }
            Registration.RETRY -> Result.retry()
            Registration.STOP -> Result.failure()
        }
    }
}

enum class Registration { DONE, RETRY, STOP }

/**
 * What a registration answer means, and the error Settings shows. 404 means
 * a server older than F097: a state, not a failure. A timeout, 429 or a
 * server error is retried. Any other answer, a refusal for the device proof
 * (403) among them, stops the worker: retrying would get the same answer for
 * ever. 405 would mean the route exists and the verb is wrong, a bug that
 * must stay visible. Pairing or the next app start runs the worker again.
 */
fun registrationOutcome(status: Int, body: String): Pair<Registration, String?> = when {
    status in 200..299 || status == 404 -> Registration.DONE to null
    status == 408 || status == 429 || status >= 500 -> Registration.RETRY to "register: HTTP $status"
    else -> Registration.STOP to when (Refusal.of(status, body)) {
        Refusal.NOT_PAIRED -> "register: this phone is not paired (HTTP 403)"
        Refusal.TOKEN_REJECTED -> "register: this device is no longer paired (HTTP 403)"
        null -> "register: HTTP $status"
    }
}

/** Diagnostics for Settings (spec §7.2). */
object TokenState {
    @Volatile var registeredAt: Long? = null
    @Volatile var lastError: String? = null
}
