// Root build. Every plugin is declared here with `apply false` so `:core`
// and `:app` resolve them from one classloader. AGP 9 has built-in Kotlin,
// so `:app` never applies `org.jetbrains.kotlin.android`; KGP is raised to
// the catalog version through the root classpath (spec §7.1).
buildscript {
    dependencies {
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:${libs.versions.kotlin.get()}")
    }
}

plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
