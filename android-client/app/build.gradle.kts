import java.net.URL
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Подпись релиза: android-client/keystore.properties (не в git) — storeFile, storePassword, keyAlias, keyPassword.
val signing = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "org.kidsspeaker"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.kidsspeaker"
        minSdk = 30
        targetSdk = 34
        versionCode = (project.findProperty("versionCode") as String?)?.toInt() ?: 1
        versionName = (project.findProperty("versionName") as String?) ?: "0.1"
        // телефоны — arm; x86_64 — только для эмулятора в отладочной сборке (ONNX Runtime весит много)
        val release = gradle.startParameter.taskNames.any { it.contains("Release") }
        ndk { abiFilters += if (release) listOf("arm64-v8a", "armeabi-v7a") else listOf("arm64-v8a", "armeabi-v7a", "x86_64") }
        // значения по умолчанию в настройках приложения
        buildConfigField("String", "DEFAULT_SERVER", "\"${project.findProperty("ksServer") ?: "https://ks.dameni.org"}\"")
    }

    signingConfigs {
        if (signing.isNotEmpty()) {
            create("release") {
                storeFile = file(signing.getProperty("storeFile"))
                storePassword = signing.getProperty("storePassword")
                keyAlias = signing.getProperty("keyAlias")
                keyPassword = signing.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (signing.isNotEmpty()) signingConfig = signingConfigs.getByName("release")
        }
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    testOptions { unitTests.isReturnDefaultValues = true }
    androidResources { noCompress += "onnx" }
}

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

// Модели openWakeWord (те же, что у pi-client): качаются при сборке, в git не хранятся.
val oww = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
val fetchModels by tasks.registering {
    val dir = file("src/main/assets")
    val names = listOf("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
    outputs.files(names.map { File(dir, it) })
    doLast {
        dir.mkdirs()
        for (n in names) {
            val f = File(dir, n)
            if (!f.exists()) URL("$oww/$n").openStream().use { input -> f.outputStream().use { input.copyTo(it) } }
        }
    }
}
tasks.named("preBuild") { dependsOn(fetchModels) }

dependencies {
    val media3 = "1.5.1"
    val ort = "1.20.0"
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.media3:media3-exoplayer:$media3")
    implementation("androidx.media3:media3-datasource:$media3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:$ort")
    implementation("com.github.gkonovalov.android-vad:webrtc:2.0.10")

    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.json:json:20240303")
    testRuntimeOnly("com.microsoft.onnxruntime:onnxruntime:$ort")   // нативная библиотека для JVM-тестов
}
