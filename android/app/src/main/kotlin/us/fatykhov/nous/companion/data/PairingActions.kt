package us.fatykhov.nous.companion.data

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import us.fatykhov.nous.companion.core.Pairing
import us.fatykhov.nous.companion.core.PairingWords
import us.fatykhov.nous.companion.core.RedeemOutcome
import us.fatykhov.nous.companion.push.Notifications

/**
 * Pair, Unpair and Disconnect, run on the app's scope rather than a screen's,
 * and one at a time.
 *
 * The app's scope: leaving Settings or turning the phone while the server
 * answers would otherwise cancel the work halfway. A code the server already
 * redeemed would leave a live device whose token the phone never stored, and
 * an unpair stopped after the push row was deleted would leave the phone
 * paired without it.
 *
 * One at a time, in the order asked: a Disconnect asked for while a pairing
 * waits for the server runs after it, and so also revokes the device that
 * pairing produced; a Pair or an Unpair that comes after a Disconnect finds
 * nothing configured and does nothing. Settings disables the three buttons
 * while any of them runs or waits ([State.busy]).
 *
 * The sync engine is started again after a change only while the app is in
 * the foreground; otherwise the next foreground starts it.
 */
class PairingActions(private val graph: AppGraph) {
    enum class Op { PAIR, UNPAIR, DISCONNECT }

    /** What Settings shows: whether work is running or waiting, which one runs now, and the last result's sentence. */
    class State(val busy: Boolean = false, val note: String = "", val isError: Boolean = false, val running: Op? = null)

    var state by mutableStateOf(State())
        private set

    private val oneAtATime = Mutex()
    private var queued = 0   // main thread only

    /** Redeem [code] at the configured address and keep the token; first make sure a token can be kept. */
    fun pair(code: String) = enqueue(Op.PAIR) {
        if (!graph.settings.configured) return@enqueue null
        if (!graph.credential.ready()) return@enqueue State(note = PairingWords.NO_KEY, isError = true)
        val base = graph.settings.baseUrl
        when (val out = Pairing.redeem(graph.http, Endpoints.of(base), code, graph.settings.installationId, graph.settings.deviceName, graph.debuggable)) {
            is RedeemOutcome.Done -> if (graph.credential.save(out.paired, base)) {
                reconnectIfForeground()
                graph.push.registerIfPossible()
                State(note = PairingWords.paired(out.paired.name))
            } else State(note = PairingWords.NOT_STORED, isError = true)
            is RedeemOutcome.Refused -> State(note = PairingWords.of(out.error), isError = true)
        }
    }

    /** Delete the push row, revoke the device, forget the token (see [Pairing.unpair]). Nothing when not paired here. */
    fun unpair() = enqueue(Op.UNPAIR) {
        if (!graph.settings.configured || graph.credential.pairedFor(graph.settings.baseUrl) == null) return@enqueue null
        val report = Pairing.unpair(graph.http, graph.settings.installationId, paired = true, forget = { graph.credential.clear() })
        reconnectIfForeground()
        if (report.revoked) State(note = PairingWords.UNPAIRED) else State(note = PairingWords.UNPAIRED_UNCONFIRMED, isError = true)
    }

    /**
     * Stop syncing, delete this installation's push row while the token still
     * works, revoke the device (when paired here), then clear the pairing and
     * every setting. Each step is bounded and best effort: a dead server must
     * not trap the owner on this phone. [onDone] runs at the end.
     */
    fun disconnect(onDone: () -> Unit) = enqueue(Op.DISCONNECT) {
        graph.engine.stop()
        graph.push.cancelRegistration()   // a registration still waiting must not put the row back
        Pairing.unpair(graph.http, graph.settings.installationId, paired = graph.credential.pairedFor(graph.settings.baseUrl) != null, forget = { graph.credential.clear() })
        Notifications.cancelAll(graph.appContext)
        graph.credential.clear()
        graph.settings.clear()
        onDone()
        State()
    }

    private fun reconnectIfForeground() { if (graph.lifecycle.inForeground) graph.engine.connect() }

    /** Run [work] after everything asked for before it. A null result keeps the last sentence. */
    private fun enqueue(op: Op, work: suspend () -> State?) {
        queued += 1
        state = State(busy = true, running = state.running)
        graph.mainScope.launch {
            var result: State? = null
            try {
                oneAtATime.withLock {
                    state = State(busy = true, running = op)
                    result = work()
                }
            } finally {
                queued -= 1
                val r = result
                state = State(busy = queued > 0, note = r?.note ?: state.note, isError = r?.isError ?: state.isError)
            }
        }
    }
}
