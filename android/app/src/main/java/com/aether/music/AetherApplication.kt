package com.aether.music

import android.app.Application
import com.aether.music.data.LibraryStore
import com.aether.music.data.YouTubeInnerTubeClient

class AetherApplication : Application() {
    lateinit var libraryStore: LibraryStore
        private set

    lateinit var youtubeClient: YouTubeInnerTubeClient
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        libraryStore = LibraryStore(this)
        youtubeClient = YouTubeInnerTubeClient()
    }

    companion object {
        @Volatile
        lateinit var instance: AetherApplication
            private set
    }
}
