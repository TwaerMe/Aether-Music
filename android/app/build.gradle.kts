plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.aether.music"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.aether.music"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "2.2.0"

        vectorDrawables {
            useSupportLibrary = true
        }
    }

    flavorDimensions += "widgetMode"
    productFlavors {
        create("originOs") {
            dimension = "widgetMode"
            // OriginOS 6 Atomic Player (原子随身听) whitelists com.spotify.music
            // so the built-in desktop music widget binds natively to our MediaLibraryService.
            applicationId = "com.spotify.music"
            resValue("string", "app_name", "Aether Music")
            buildConfigField("Boolean", "IS_ORIGIN_OS_WIDGET_BUILD", "true")
        }
        create("standard") {
            dimension = "widgetMode"
            applicationId = "com.aether.music"
            resValue("string", "app_name", "Aether Music")
            buildConfigField("Boolean", "IS_ORIGIN_OS_WIDGET_BUILD", "false")
        }
    }

    signingConfigs {
        getByName("debug") {
            enableV1Signing = true
            enableV2Signing = true
            enableV3Signing = true
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.activity:activity-compose:1.9.2")

    implementation(platform("androidx.compose:compose-bom:2024.09.03"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    // Media3 (ExoPlayer + MediaLibraryService for OriginOS 6 Atomic Player & Origin Island)
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-session:1.4.1")
    implementation("androidx.media3:media3-common:1.4.1")
    implementation("androidx.media3:media3-datasource-okhttp:1.4.1")

    // Networking & Artwork
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("io.coil-kt:coil-compose:2.7.0")

    // Coroutines + Guava ListenableFuture interop for MediaController
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-guava:1.9.0")
}
