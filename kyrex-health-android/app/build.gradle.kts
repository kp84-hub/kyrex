plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
android {
    namespace = "com.kyrex.health"
    compileSdk = 36
    defaultConfig { applicationId = "com.kyrex.health"; minSdk = 28; targetSdk = 35; versionCode = 5; versionName = "0.5" }
    System.getenv("KYREX_HEALTH_DEBUG_KEYSTORE")?.let { signingConfigs.getByName("debug").storeFile = file(it) }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    testOptions {
        unitTests.isIncludeAndroidResources = true
        unitTests.all { test ->
            // Let Robolectric dependency resolution honor the Gradle JVM's proxy configuration.
            listOf("http.proxyHost", "http.proxyPort", "https.proxyHost", "https.proxyPort",
                "robolectric.dependency.repo.url").forEach { name ->
                System.getProperty(name)?.let { test.systemProperty(name, it) }
            }
        }
    }
    kotlinOptions { jvmTarget = "17" }
}
dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.14.1")
    testImplementation("androidx.work:work-testing:2.10.1")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.0")
    implementation("androidx.health.connect:connect-client:1.1.0")
}
