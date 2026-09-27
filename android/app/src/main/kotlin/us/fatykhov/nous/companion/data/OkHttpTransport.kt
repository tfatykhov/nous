package us.fatykhov.nous.companion.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import us.fatykhov.nous.companion.core.Http
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * `:core.Http` over OkHttp (spec §7.3). Three clients: the stream (45 s read
 * = 3× the 15 s keepalive, no call timeout), `/a2ui/call` (200 s: refine
 * runs up to three 60 s compose rounds) and everything else (15 s). The
 * stream never sends `Last-Event-ID` (R2) and returns on any failure.
 */
class OkHttpTransport(private val settings: Settings) : Http {
    private val plain = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()
    private val call = plain.newBuilder().readTimeout(200, TimeUnit.SECONDS).build()
    private val stream = plain.newBuilder().readTimeout(45, TimeUnit.SECONDS).callTimeout(0, TimeUnit.SECONDS).build()
    @Volatile private var activeStream: Call? = null

    private fun url(path: String) = settings.baseUrl + path

    override suspend fun get(path: String): Http.Response =
        plain.newCall(Request.Builder().url(url(path)).get().build()).await().toCore()

    override suspend fun postJson(path: String, body: String): Http.Response {
        val client = if (path == "/a2ui/call") call else plain
        val req = Request.Builder().url(url(path)).post(body.toRequestBody("application/json".toMediaType())).build()
        return client.newCall(req).await().toCore()
    }

    override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(url(path)).header("Accept", "text/event-stream").get().build()
        val c = stream.newCall(req)
        activeStream = c
        try {
            c.execute().use { res ->
                if (!res.isSuccessful) return@use
                onOpen()
                val src = res.body.source()
                val buf = ByteArray(8 * 1024)
                while (true) {
                    val n = src.read(buf)
                    if (n < 0) break
                    onChunk(String(buf, 0, n, Charsets.UTF_8))
                }
            }
        } catch (_: IOException) {
            // EOF, timeout, or a network switch cancelled us: the engine reruns R1.
        } finally {
            if (activeStream === c) activeStream = null
        }
    }

    /** Cancel the live stream so the engine reconnects at once (network switch). */
    fun cancelStream() { activeStream?.cancel() }

    private fun Response.toCore(): Http.Response = use {
        Http.Response(code, body.string(), headers.names().associateWith { headers[it] ?: "" })
    }

    private suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { if (cont.isActive) cont.resumeWithException(e) }
            override fun onResponse(call: Call, response: Response) { cont.resume(response) }
        })
        cont.invokeOnCancellation { cancel() }
    }
}
