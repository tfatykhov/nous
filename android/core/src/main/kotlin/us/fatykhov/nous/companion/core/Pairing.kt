package us.fatykhov.nous.companion.core

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Device pairing: the owner opens a code in the dashboard, the app redeems it
 * once at `POST /a2ui/devices/redeem` and is given its own device token. From
 * then on the app sends the token as [DeviceToken.HEADER] to the origin it
 * paired with, and only there (see [Origin] and [TokenPolicy]).
 *
 * Pure Kotlin: the app binds the HTTP client, the Keystore box and the files.
 * Nothing here ever puts the token or the code into a string meant for a
 * person or a log.
 */
object DeviceToken {
    const val HEADER = "X-Nous-Device-Token"
    const val REDEEM_PATH = "/a2ui/devices/redeem"
    const val UNPAIR_PATH = "/a2ui/devices/unpair"
    /** The shape the server issues: `nd1_` and 43 URL-safe characters. */
    fun wellFormed(token: String): Boolean =
        token.length == 47 && token.startsWith("nd1_") &&
            token.drop(4).all { it in 'A'..'Z' || it in 'a'..'z' || it in '0'..'9' || it == '-' || it == '_' }
}

/** The code the dashboard shows, as typed: any case, with or without the dash or spaces. */
object PairingCode {
    /** Upper case with spaces and dashes removed; null unless 8 letters and digits remain. */
    fun normalise(input: String): String? {
        val c = input.filterNot { it.isWhitespace() || it == '-' }.uppercase()
        return c.takeIf { it.length == 8 && it.all { ch -> ch in 'A'..'Z' || ch in '0'..'9' } }
    }
}

/**
 * An origin as `scheme://host:port`, scheme and host in lower case. Two
 * addresses are the same server for the device token only when their
 * origins are equal.
 */
object Origin {
    fun of(scheme: String, host: String, port: Int): String =
        scheme.lowercase() + "://" + host.lowercase().removePrefix("[").removeSuffix("]") + ":" + port
}

/**
 * An address as the app's HTTP client parses it: scheme, canonical host and
 * port (the default filled in). `:core` parses no address itself, so the
 * origin stored at pairing and the one each request is checked against come
 * from the same parser.
 */
data class Endpoint(val scheme: String, val host: String, val port: Int) {
    val origin: String get() = Origin.of(scheme, host, port)
}

/**
 * Where the device token may go: https anywhere; plain http only to the
 * emulator's loopback names (`10.0.2.2`, `localhost`) and only on a
 * debuggable build, so a local Nous can be used for development.
 */
object TokenPolicy {
    private val EMULATOR_HOSTS = setOf("10.0.2.2", "localhost")

    fun mayCarry(scheme: String, host: String, debuggable: Boolean): Boolean = when (scheme.lowercase()) {
        "https" -> true
        "http" -> debuggable && host.lowercase() in EMULATOR_HOSTS
        else -> false
    }
}

/** The two refusals the server gives for a missing or unknown device proof. */
enum class Refusal {
    /** `OWNER_PROOF_REQUIRED`: the call carried no proof the server accepted. */
    NOT_PAIRED,
    /** `DEVICE_TOKEN_REJECTED`: the token the call carried belongs to no live device. */
    TOKEN_REJECTED;

    companion object {
        /**
         * The refusal in a 403 answer, or null. The code sits in `error.code`
         * (`/a2ui/action`, the device routes) or in
         * `agentFunctionResponse.error.code` (`/a2ui/call`); an `error` that is
         * a plain string carries no code.
         */
        fun of(status: Int, body: String): Refusal? {
            if (status != 403) return null
            val o = try { Json.parseToJsonElement(body) as? JsonObject } catch (_: Exception) { null } ?: return null
            val err = (o["error"] as? JsonObject)
                ?: ((o["agentFunctionResponse"] as? JsonObject)?.get("error") as? JsonObject)
                ?: return null
            return when (err["code"]?.stringOrNull) {
                "OWNER_PROOF_REQUIRED" -> NOT_PAIRED
                "DEVICE_TOKEN_REJECTED" -> TOKEN_REJECTED
                else -> null
            }
        }
    }
}

/** What a redeem gave back. Not a data class: its generated toString would print the token. */
class Paired(val token: String, val deviceId: String, val name: String, val publicBaseUrl: String?) {
    override fun toString(): String = "Paired(deviceId=$deviceId, name=$name)"
}

/** Why a pairing did not happen; each has its own sentence in [PairingWords]. */
sealed interface PairingError {
    /** The typed text is not 8 letters and digits; nothing was sent. */
    data object Malformed : PairingError
    /** The server's one answer for a wrong, expired, used or closed code. */
    data object NotValid : PairingError
    /** `429`: the server's redeem limit is full; [retryAfterSeconds] from `Retry-After`. */
    data class TooManyTries(val retryAfterSeconds: Int?) : PairingError
    /** The server does not know the route: a Nous older than pairing. */
    data object Unsupported : PairingError
    /** The HTTP client cannot parse the configured address; nothing was sent. */
    data object BadAddress : PairingError
    /** The configured address is not one the token may be sent to; nothing was sent. */
    data object NoHttps : PairingError
    /** No answer at all. */
    data object Unreachable : PairingError
    /** Any other answer. */
    data class Failed(val status: Int) : PairingError
}

sealed interface RedeemOutcome {
    class Done(val paired: Paired) : RedeemOutcome {
        override fun toString(): String = "Done($paired)"
    }
    data class Refused(val error: PairingError) : RedeemOutcome
}

/** What an unpair did: [pushDeleted] and [revoked] are what the server confirmed. */
data class UnpairReport(val pushDeleted: Boolean, val revoked: Boolean)

/** Every sentence the pairing flow shows. None contains a token or a code. */
object PairingWords {
    const val PAIRED_PREFIX = "Paired as "
    const val NOT_PAIRED = "Not paired."
    const val NOT_PAIRED_HINT = "Pairing lets this phone approve and answer cards. In the dashboard open Integrations, Devices, Show a code, then type the code here."
    const val TAP_NOT_PAIRED = "Pair this device to use this button."
    const val TOKEN_REJECTED = "This device is no longer paired. Pair it again in Settings."
    const val NOT_CHECKED = "Nous could not check this device. Try again."
    const val INBOX_NOT_PAIRED_TITLE = "This phone is not paired"
    const val INBOX_NOT_PAIRED = "Nous shows cards only to a paired phone. Pair it in Settings with a code from the dashboard."
    const val INBOX_UNPAIRED_TITLE = "This phone is no longer paired"
    const val UNPAIRED = "Unpaired."
    const val UNPAIRED_UNCONFIRMED = "Unpaired on this phone. Nous did not confirm it, so revoke this device in the dashboard."
    const val NOT_STORED = "This phone could not store its device key, so it is not paired. Revoke the new device in the dashboard, then try again."

    fun paired(name: String) = "$PAIRED_PREFIX$name."

    fun of(error: PairingError): String = when (error) {
        PairingError.Malformed -> "Type the 8 letters and digits the dashboard shows, like K7QM-2XRD."
        PairingError.NotValid -> "That code is not valid or has run out. Show a new one in the dashboard."
        is PairingError.TooManyTries -> "Too many tries. " + (error.retryAfterSeconds?.let { "Wait $it seconds, then try again." } ?: "Wait a minute, then try again.")
        PairingError.Unsupported -> "This server does not support pairing yet."
        PairingError.NoHttps -> "Pairing needs the https address of Nous. Disconnect, then connect again with the https address."
        PairingError.BadAddress -> "This address cannot be used for pairing. Disconnect, then connect again with the https address of Nous."
        PairingError.Unreachable -> "Couldn't reach Nous. Check Tailscale and try again."
        is PairingError.Failed -> "Pairing did not work (HTTP ${error.status}). Try again."
    }
}

object Pairing {
    const val BOUND_MS = 5_000L

    /**
     * Redeem [code] at the configured address, [endpoint] as the HTTP client
     * parsed it (null when it could not). Refuses before sending anything when
     * the address cannot be parsed or may not carry a token, or the code is not
     * 8 letters and digits.
     */
    suspend fun redeem(http: Http, endpoint: Endpoint?, code: String, installationId: String, name: String, debuggable: Boolean): RedeemOutcome {
        if (endpoint == null) return RedeemOutcome.Refused(PairingError.BadAddress)
        if (!TokenPolicy.mayCarry(endpoint.scheme, endpoint.host, debuggable)) return RedeemOutcome.Refused(PairingError.NoHttps)
        val normal = PairingCode.normalise(code) ?: return RedeemOutcome.Refused(PairingError.Malformed)
        val body = buildJsonObject { put("code", normal); put("installation_id", installationId); put("name", name) }
        val res = try { http.postJson(DeviceToken.REDEEM_PATH, body.toString()) }
        catch (e: CancellationException) { throw e }
        catch (_: Exception) { return RedeemOutcome.Refused(PairingError.Unreachable) }
        val o = try { Json.parseToJsonElement(res.body) as? JsonObject } catch (_: Exception) { null }
        if (res.status == 200 && o != null) {
            val token = o["token"]?.stringOrNull
            val id = o["device_id"]?.stringOrNull
            if (token != null && id != null && DeviceToken.wellFormed(token)) {
                val public = o["public_base_url"]?.stringOrNull?.trim()?.trimEnd('/')
                    ?.takeIf { it.startsWith("https://") || it.startsWith("http://") }
                return RedeemOutcome.Done(Paired(token, id, o["name"]?.stringOrNull ?: name, public))
            }
        }
        val error = when (res.status) {
            // The route's own 404 is JSON with a string `error`; a server without
            // the route answers a plain "Not Found" (or a proxy's page).
            404 -> if ((o?.get("error") as? JsonPrimitive)?.isString == true) PairingError.NotValid else PairingError.Unsupported
            405 -> PairingError.Unsupported
            429 -> PairingError.TooManyTries(res.header("Retry-After")?.trim()?.toIntOrNull()?.takeIf { it > 0 })
            else -> PairingError.Failed(res.status)
        }
        return RedeemOutcome.Refused(error)
    }

    /**
     * Unpair, in this order, each step bounded and best effort: delete this
     * installation's push registration while the token still works, revoke
     * the device at the server (only when [paired]), then [forget] the token
     * whatever the server answered, and also when the caller is cancelled
     * halfway (a phone left paired without its push row is the worse state).
     */
    suspend fun unpair(http: Http, installationId: String, paired: Boolean, forget: () -> Unit, boundMs: Long = BOUND_MS): UnpairReport {
        var push: Http.Response? = null
        var revoke: Http.Response? = null
        try {
            push = attempt(boundMs) { http.delete("/a2ui/push/tokens/" + encodeURIComponent(installationId)) }
            if (paired) revoke = attempt(boundMs) { http.postJson(DeviceToken.UNPAIR_PATH, "{}") }
        } finally {
            forget()
        }
        return UnpairReport(
            pushDeleted = push?.ok == true,
            // A token the server already rejects is as revoked as one it just revoked.
            revoked = revoke != null && (revoke.ok || Refusal.of(revoke.status, revoke.body) == Refusal.TOKEN_REJECTED),
        )
    }

    private suspend fun attempt(boundMs: Long, call: suspend () -> Http.Response): Http.Response? = try {
        withTimeout(boundMs) { call() }
    } catch (e: CancellationException) {
        // withTimeout's own cancellation is a step that ran out of time; any other is the caller's.
        if (e is kotlinx.coroutines.TimeoutCancellationException) null else throw e
    } catch (_: Exception) { null }
}

/** A Keystore-backed (or, in tests, fake) encrypt and decrypt. Null on any failure. */
interface SecretBox {
    class Sealed(val ciphertext: ByteArray, val iv: ByteArray)
    fun seal(plain: ByteArray): Sealed?
    fun open(sealed: Sealed): ByteArray?
}
