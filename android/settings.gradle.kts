// F097 — Nous Companion (Android). `:core` is pure Kotlin/JVM and always
// builds; `:app` needs an Android SDK and is included only when one is
// found (spec §7.1), so a machine with just a JDK can still run the
// `:core` tests. CI names `:app:*` tasks explicitly, so a missing SDK there
// fails loudly instead of silently building less.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "nous-companion"

include(":core")

val sdkDir: String? = run {
    val local = file("local.properties")
    val fromLocal = if (local.isFile) {
        java.util.Properties().apply { local.inputStream().use { load(it) } }.getProperty("sdk.dir")
    } else null
    fromLocal
        ?: providers.environmentVariable("ANDROID_HOME").orNull
        ?: providers.environmentVariable("ANDROID_SDK_ROOT").orNull
}
val includeApp: Boolean =
    providers.gradleProperty("nous.includeApp").orNull?.toBoolean()
        ?: (sdkDir != null && file(sdkDir).isDirectory)

if (includeApp) {
    include(":app")
} else {
    logger.lifecycle("No Android SDK: :app excluded; only :core builds")
}
