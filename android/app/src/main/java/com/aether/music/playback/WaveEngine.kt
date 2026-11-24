package com.aether.music.playback

import com.aether.music.data.LibraryStore
import com.aether.music.data.Track
import com.aether.music.data.WaveMoods
import com.aether.music.data.YouTubeInnerTubeClient
import kotlin.random.Random

class WaveEngine(
    private val youtube: YouTubeInnerTubeClient,
    private val store: LibraryStore,
) {
    fun buildWaveTracks(
        mood: String,
        excludeVideoIds: Set<String> = emptySet(),
        desiredCount: Int = 18,
    ): Pair<List<Track>, String> {
        val state = store.current
        val blocked = state.dislikedIds + excludeVideoIds
        val moodMod = WaveMoods.modifiers[mood].orEmpty()

        val seeds = (state.favorites.take(15) + state.history.take(10)).distinctBy { it.videoId }
        val result = mutableListOf<Track>()
        var reason = "Подборка «Моя волна»"

        if (seeds.isNotEmpty() && mood == "Любое") {
            val picked = seeds[Random.nextInt(seeds.size)]
            reason = "Похоже на ${picked.artist}"
            val mix = youtube.fetchRadioMix(picked.videoId, limit = desiredCount + 6)
            result += mix
        } else if (seeds.isNotEmpty()) {
            val picked = seeds[Random.nextInt(seeds.size)]
            reason = "${picked.artist} · $mood"
            val q = "${picked.artist} $moodMod music".trim()
            result += youtube.searchTracks(q, limit = desiredCount + 6)
        }

        if (result.size < 6) {
            val queries = WaveMoods.fallbackQueries[mood]
                ?: WaveMoods.fallbackQueries["Любое"]!!
            val chosen = queries[Random.nextInt(queries.size)]
            if (result.isEmpty()) {
                reason = if (mood == "Любое") "Волна открытий" else "Настроение: $mood"
            }
            result += youtube.searchTracks(chosen, limit = desiredCount + 6)
        }

        val filtered = result
            .filter { it.videoId !in blocked }
            .distinctBy { it.videoId }
            .shuffled()
            .take(desiredCount)

        return filtered to reason
    }
}
