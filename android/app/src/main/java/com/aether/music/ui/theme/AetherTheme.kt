package com.aether.music.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

object AetherColors {
    val BgRoot = Color(0xFF0D0F12)
    val BgNav = Color(0xFF111419)
    val BgBar = Color(0xFF14181F)
    val BgCard = Color(0xFF181D25)
    val BgCardAlt = Color(0xFF1D232D)
    val BgHover = Color(0xFF232A36)
    val Border = Color(0xFF232934)
    val TextMain = Color(0xFFF2F4F8)
    val TextMuted = Color(0xFF9099A8)
    val TextDim = Color(0xFF5A6372)
    val WaveGold = Color(0xFFC8A464)
    val WaveSoft = Color(0xFF28241C)
    val LikeRed = Color(0xFFCC6B6B)
    val OkGreen = Color(0xFF6BA882)
}

private val AetherDarkScheme = darkColorScheme(
    primary = AetherColors.WaveGold,
    onPrimary = AetherColors.BgRoot,
    secondary = AetherColors.WaveSoft,
    onSecondary = AetherColors.WaveGold,
    background = AetherColors.BgRoot,
    onBackground = AetherColors.TextMain,
    surface = AetherColors.BgCard,
    onSurface = AetherColors.TextMain,
    surfaceVariant = AetherColors.BgCardAlt,
    onSurfaceVariant = AetherColors.TextMuted,
    outline = AetherColors.Border,
    error = AetherColors.LikeRed,
)

@Composable
fun AetherTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = AetherDarkScheme,
        content = content,
    )
}
