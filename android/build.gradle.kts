// Root build. Every plugin is declared here with `apply false` so `:core`
// and `:app` resolve them from one classloader (AGP 9 built-in Kotlin
// requires KGP on the root classpath — spec §7.1).
plugins {
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
