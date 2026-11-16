package us.fatykhov.nous.companion.data

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import us.fatykhov.nous.companion.core.Endpoint

/**
 * The configured address as OkHttp parses it, or null when OkHttp cannot.
 * Requests go through the same parser, so the origin stored at pairing and
 * the one the interceptor checks each request against always agree (an IPv6
 * literal written out in full, a host with an underscore, a non-ASCII host).
 */
object Endpoints {
    fun of(url: String): Endpoint? = url.trim().toHttpUrlOrNull()?.let { Endpoint(it.scheme, it.host, it.port) }
}
