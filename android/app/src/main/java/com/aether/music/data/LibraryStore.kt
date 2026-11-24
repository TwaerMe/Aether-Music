package com.aether.music.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

data class UserLibraryState(
    val favorites: List<Track> = emptyList(),
    val history: List<Track> = emptyList(),
    val lastQueue: List<Track> = emptyList(),
    val dislikedIds: Set<String> = emptySet(),
    val waveMood: String = "Любое",
    val eqPreset: EqPreset = EqPreset.FLAT,
    val recentSearches: List<String> = emptyList(),
)

class LibraryStore(context: Context) {
    private val stateFile = File(context.filesDir, "aether_mobile_state.json")
    private val lock = Any()

    private val _state = MutableStateFlow(loadState())
    val state: StateFlow<UserLibraryState> = _state.asStateFlow()

    val current: UserLibraryState
        get() = _state.value

    fun isFavorite(videoId: String): Boolean =
        _state.value.favorites.any { it.videoId == videoId }

    fun toggleFavorite(track: Track): Boolean {
        var added = false
        updateState { cur ->
            val exists = cur.favorites.any { it.videoId == track.videoId }
            if (exists) {
                added = false
                cur.copy(favorites = cur.favorites.filterNot { it.videoId == track.videoId })
            } else {
                added = true
                cur.copy(
                    favorites = (listOf(track) + cur.favorites).distinctBy { it.videoId }.take(500),
                    dislikedIds = cur.dislikedIds - track.videoId,
                )
            }
        }
        return added
    }

    fun dislikeTrack(videoId: String) {
        updateState { cur ->
            cur.copy(
                dislikedIds = (cur.dislikedIds + videoId),
                favorites = cur.favorites.filterNot { it.videoId == videoId },
            )
        }
    }

    fun recordPlay(track: Track) {
        updateState { cur ->
            val updatedHistory = (listOf(track) + cur.history.filterNot { it.videoId == track.videoId })
                .take(150)
            cur.copy(history = updatedHistory)
        }
    }

    fun saveLastQueue(tracks: List<Track>) {
        if (tracks.isEmpty()) return
        updateState { cur ->
            cur.copy(lastQueue = tracks.take(40))
        }
    }

    fun setWaveMood(mood: String) {
        val valid = if (mood in WaveMoods.all) mood else "Любое"
        updateState { cur -> cur.copy(waveMood = valid) }
    }

    fun setEqPreset(preset: EqPreset) {
        updateState { cur -> cur.copy(eqPreset = preset) }
    }

    fun addRecentSearch(query: String) {
        val clean = query.trim()
        if (clean.isEmpty()) return
        updateState { cur ->
            val updated = (listOf(clean) + cur.recentSearches.filterNot { it.equals(clean, ignoreCase = true) })
                .take(12)
            cur.copy(recentSearches = updated)
        }
    }

    private fun updateState(transform: (UserLibraryState) -> UserLibraryState) {
        synchronized(lock) {
            val next = transform(_state.value)
            _state.value = next
            persistLocked(next)
        }
    }

    private fun loadState(): UserLibraryState {
        if (!stateFile.exists()) return UserLibraryState()
        return try {
            val root = JSONObject(stateFile.readText(Charsets.UTF_8))
            val favorites = parseTrackList(root.optJSONArray("favorites"))
            val history = parseTrackList(root.optJSONArray("history"))
            val lastQueue = parseTrackList(root.optJSONArray("lastQueue"))
            val dislikedArr = root.optJSONArray("dislikedIds")
            val disliked = buildSet {
                if (dislikedArr != null) {
                    for (i in 0 until dislikedArr.length()) {
                        val id = dislikedArr.optString(i).trim()
                        if (id.isNotEmpty()) add(id)
                    }
                }
            }
            val searchesArr = root.optJSONArray("recentSearches")
            val searches = buildList {
                if (searchesArr != null) {
                    for (i in 0 until searchesArr.length()) {
                        val q = searchesArr.optString(i).trim()
                        if (q.isNotEmpty()) add(q)
                    }
                }
            }
            val mood = root.optString("waveMood", "Любое").let {
                if (it in WaveMoods.all) it else "Любое"
            }
            val eq = EqPreset.fromId(root.optString("eqPreset", "flat"))
            UserLibraryState(
                favorites = favorites,
                history = history,
                lastQueue = lastQueue,
                dislikedIds = disliked,
                waveMood = mood,
                eqPreset = eq,
                recentSearches = searches,
            )
        } catch (_: Exception) {
            UserLibraryState()
        }
    }

    private fun parseTrackList(arr: JSONArray?): List<Track> {
        if (arr == null) return emptyList()
        return buildList {
            for (i in 0 until arr.length()) {
                Track.fromJson(arr.optJSONObject(i))?.let { add(it) }
            }
        }
    }

    private fun persistLocked(st: UserLibraryState) {
        try {
            val root = JSONObject().apply {
                put("v", 1)
                put("waveMood", st.waveMood)
                put("eqPreset", st.eqPreset.id)
                put("favorites", JSONArray().apply { st.favorites.forEach { put(it.toJson()) } })
                put("history", JSONArray().apply { st.history.forEach { put(it.toJson()) } })
                put("lastQueue", JSONArray().apply { st.lastQueue.forEach { put(it.toJson()) } })
                put("dislikedIds", JSONArray().apply { st.dislikedIds.forEach { put(it) } })
                put("recentSearches", JSONArray().apply { st.recentSearches.forEach { put(it) } })
            }
            val tmp = File(stateFile.parentFile, "${stateFile.name}.tmp")
            tmp.writeText(root.toString(), Charsets.UTF_8)
            if (!tmp.renameTo(stateFile)) {
                stateFile.writeText(root.toString(), Charsets.UTF_8)
                tmp.delete()
            }
        } catch (_: Exception) {
            // Keep running smoothly if storage is temporarily unavailable
        }
    }
}
