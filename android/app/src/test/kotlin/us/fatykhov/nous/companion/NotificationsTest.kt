package us.fatykhov.nous.companion

import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import us.fatykhov.nous.companion.push.Notifications
import us.fatykhov.nous.companion.push.PushManager
import us.fatykhov.nous.companion.push.Tombstones

/** Spec §10.4: post/cancel by tag, channel mapping, tombstones, reconcile. */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
class NotificationsTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    @Test fun channelMapping() {
        assertEquals(PushManager.CHANNEL_APPROVALS, PushManager.channelFor(2))
        assertEquals(PushManager.CHANNEL_UPDATES, PushManager.channelFor(1))
        assertEquals(PushManager.CHANNEL_GENERAL, PushManager.channelFor(0))
    }

    @Test fun cancelByTagAndReconcile() {
        val n = android.app.Notification.Builder(ctx, PushManager.CHANNEL_UPDATES).setContentTitle("t").build()
        nm.notify("s1", PushManager.NOTIF_ID, n); nm.notify("s2", PushManager.NOTIF_ID, n)
        assertEquals(2, shadowOf(nm).size())
        Notifications.cancel(ctx, "s1")
        assertEquals(1, shadowOf(nm).size())
        Notifications.reconcile(ctx, liveIds = emptySet())
        assertEquals(0, shadowOf(nm).size())
    }

    @Test fun tombstonesExpireAndCap() {
        Tombstones.add(ctx, "a")
        assertTrue(Tombstones.contains(ctx, "a"))
        assertFalse(Tombstones.contains(ctx, "b"))
        for (i in 0 until 600) Tombstones.add(ctx, "x$i")
        assertTrue(ctx.getSharedPreferences("tombstones", Context.MODE_PRIVATE).all.size <= 500)
    }
}
