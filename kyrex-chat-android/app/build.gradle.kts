import java.net.URI

plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
val chatOrigin = providers.gradleProperty("kyrexChatOrigin").get().trimEnd('/')
val origin = URI(chatOrigin)
require(origin.scheme == "https" && !origin.host.isNullOrBlank() && origin.rawUserInfo == null &&
    origin.rawQuery == null && origin.rawFragment == null && origin.rawPath.isNullOrEmpty()) {
    "Set -PkyrexChatOrigin to your trusted HTTPS Kyrex Chat origin."
}
android {
    namespace = "com.kyrex.chat"
    compileSdk = 36
    buildFeatures { buildConfig = true }
    defaultConfig {
        applicationId = "com.kyrex.chat"; minSdk = 28; targetSdk = 35
        versionCode = 2; versionName = "0.2"
        ndk { abiFilters += "arm64-v8a" }
        buildConfigField("String", "CHAT_ORIGIN", "\"$chatOrigin\"")
        manifestPlaceholders["chatOrigin"] = chatOrigin
        resValue("string", "asset_statements", """[{"relation":["delegate_permission/common.handle_all_urls"],"target":{"namespace":"web","site":"$chatOrigin"}}]""".replace("\"", "\\\""))
        buildConfigField("String", "SOURCE_URL", "\"https://github.com/kp84-hub/kyrex\"")
    }
    // Reuse the tested Health adapter, permissions UI and worker. No copied
    // health-reading implementation or second ingestion API.
    sourceSets.getByName("main") {
        java.srcDir("../../kyrex-health-android/app/src/main/java")
        res.srcDir("../../kyrex-health-android/app/src/main/res")
        java.srcDir("../../kyrex-voice-android/app/src/main/java")
        java.srcDir("../../kyrex-messages-pairing-android/app/src/main/java")
        assets.srcDir("../../kyrex-voice-android/app/src/main/assets")
        jniLibs.srcDir("../../kyrex-voice-android/app/src/main/jniLibs")
    }
    sourceSets.getByName("test").java.srcDir("../../kyrex-health-android/app/src/test/java")
    sourceSets.getByName("test").java.srcDir("../../kyrex-voice-android/app/src/test/java")
    sourceSets.getByName("test").java.srcDir("../../kyrex-messages-pairing-android/app/src/test/java")
    System.getenv("KYREX_CHAT_ANDROID_KEYSTORE")?.let {
        signingConfigs.getByName("debug").storeFile = file(it)
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    testOptions { unitTests.isIncludeAndroidResources = true }
}
dependencies {
    implementation("com.google.androidbrowserhelper:androidbrowserhelper:2.6.2")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.0")
    implementation("androidx.health.connect:connect-client:1.1.0")
    testImplementation("junit:junit:4.13.2")
    implementation(files("../../kyrex-messages-pairing-android/app/libs/pairbridge.aar"))
    testImplementation("org.robolectric:robolectric:4.16")
    testImplementation("androidx.work:work-testing:2.10.1")
}
