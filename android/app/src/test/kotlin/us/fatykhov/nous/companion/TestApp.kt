package us.fatykhov.nous.companion

import android.app.Application

/** Keeps Firebase and WorkManager out of unit tests (spec §10.4). */
class TestApp : Application()
