plugins { id("com.android.application") }
android {
    namespace = "com.kyrex.pairing"
    compileSdk = 36
    defaultConfig { applicationId = "com.kyrex.messages.pairingtest"; minSdk = 28; targetSdk = 35; versionCode = 2; versionName = "0.2" }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
}
dependencies {
    implementation(files("libs/pairbridge.aar"))
    testImplementation("junit:junit:4.13.2")
}
