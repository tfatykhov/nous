package us.fatykhov.nous.companion.data

import okhttp3.Interceptor
import okhttp3.Response
import us.fatykhov.nous.companion.core.DeviceToken
import us.fatykhov.nous.companion.core.Refusal
import us.fatykhov.nous.companion.core.TokenPolicy

/**
 * Puts the device token on a request as `X-Nous-Device-Token`, and only when
 * all three hold: the request is the call's own (not a follow-up a redirect
 * made), its origin is the one the token was paired with, and [TokenPolicy]
 * lets the token travel there. Any such header already on the request is
 * removed first.
 *
 * It is a NETWORK interceptor: OkHttp builds a redirect's follow-up from the
 * request before network interceptors ran, so the header never rides into a
 * follow-up. The app's clients also follow no redirect at all.
 *
 * When the server answers `DEVICE_TOKEN_REJECTED` to a request that carried
 * the token, the token is forgotten here, so every caller (the sync loop,
 * taps, the push worker, Settings) gets the same handling.
 */
class DeviceTokenInterceptor(private val credential: DeviceCredential, private val debuggable: Boolean) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request().newBuilder().removeHeader(DeviceToken.HEADER).build()
        val url = request.url
        val token = if (url == chain.call().request().url && TokenPolicy.mayCarry(url.scheme, url.host, debuggable)) {
            credential.tokenFor(url.scheme, url.host, url.port)
        } else null
        if (token == null) return chain.proceed(request)
        val response = chain.proceed(request.newBuilder().header(DeviceToken.HEADER, token).build())
        // The body is read as sent: a refusal compressed in transit would not parse here, so it would not be recognised and the token would be kept.
        if (response.code == 403 && Refusal.of(403, response.peekBody(PEEK_BYTES).string()) == Refusal.TOKEN_REJECTED) {
            credential.clear(onlyIf = token)
        }
        return response
    }

    private companion object { const val PEEK_BYTES = 8_192L }
}
