package us.fatykhov.nous.companion.core

/**
 * Which hosts count as "the local network" for Android 16+'s runtime
 * `ACCESS_LOCAL_NETWORK` permission (spec §5). A Nous reached over Tailscale
 * sits in CGNAT space (100.64/10) or under `*.ts.net`, which the platform
 * treats exactly like a LAN address — so the first-run probe must ask for the
 * permission before it can succeed, not after it has already failed.
 *
 * Pure so it is testable; the platform check itself lives in the app.
 */
object Net {
    private val LOCAL_SUFFIXES = listOf(".local", ".ts.net", ".internal", ".lan", ".home", ".localdomain")

    /** The host part of a URL the user typed — no scheme, userinfo, port or path. */
    fun hostOf(url: String): String {
        var s = url.trim()
        val scheme = s.indexOf("://")
        if (scheme >= 0) s = s.substring(scheme + 3)
        s = s.substringBefore('/').substringBefore('?').substringBefore('#')
        val at = s.lastIndexOf('@')
        if (at >= 0) s = s.substring(at + 1)
        if (s.startsWith("[")) return s.substringBefore(']').drop(1).lowercase()   // [v6]:port
        return s.substringBefore(':').lowercase()
    }

    fun isLocalNetworkHost(host: String): Boolean {
        val h = host.trim().lowercase()
        if (h.isEmpty()) return false
        if (h == "localhost") return true
        ipv4(h)?.let { (a, b) ->
            return a == 10 || a == 127 ||
                (a == 172 && b in 16..31) ||
                (a == 192 && b == 168) ||
                (a == 169 && b == 254) ||
                (a == 100 && b in 64..127)   // CGNAT — Tailscale's 100.x addresses
        }
        if (h.contains(':')) {   // IPv6 literal
            return h == "::1" || h.startsWith("fe8") || h.startsWith("fe9") || h.startsWith("fea") ||
                h.startsWith("feb") || h.startsWith("fc") || h.startsWith("fd")
        }
        if (LOCAL_SUFFIXES.any { h.endsWith(it) }) return true
        return !h.contains('.')   // a bare hostname resolves on the local network
    }

    private fun ipv4(h: String): Pair<Int, Int>? {
        val parts = h.split('.')
        if (parts.size != 4 || parts.any { p -> p.isEmpty() || p.any { !it.isDigit() } || p.toInt() > 255 }) return null
        return parts[0].toInt() to parts[1].toInt()
    }
}
