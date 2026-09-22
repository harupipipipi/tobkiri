import 'package:flutter/material.dart';

ThemeData buildRumiTheme({bool dark = true}) {
  const seed = Color(0xFF8E8E93);
  final scheme = ColorScheme.fromSeed(
    seedColor: seed,
    brightness: dark ? Brightness.dark : Brightness.light,
  );

  final base = ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    brightness: dark ? Brightness.dark : Brightness.light,
    scaffoldBackgroundColor:
        dark ? const Color(0xFF0E1116) : const Color(0xFFF7F8F9),
    canvasColor: dark ? const Color(0xFF14181E) : Colors.white,
    splashFactory: InkSparkle.splashFactory,
    appBarTheme: AppBarTheme(
      centerTitle: false,
      elevation: 0,
      scrolledUnderElevation: 0.5,
      backgroundColor: dark ? const Color(0xFF0E1116) : const Color(0xFFF7F8F9),
      foregroundColor: dark ? Colors.white : Colors.black87,
      surfaceTintColor: Colors.transparent,
    ),
    cardTheme: CardThemeData(
      elevation: 0,
      color: dark ? const Color(0xFF171717) : Colors.white,
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: BorderSide(
            color: dark ? const Color(0xFF2A2A2A) : const Color(0xFFE5E7EB)),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(
            color: dark ? const Color(0xFF333333) : const Color(0xFFD1D5DB)),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(
            color: dark ? const Color(0xFF333333) : const Color(0xFFD1D5DB)),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(color: scheme.primary, width: 1.5),
      ),
      filled: true,
      fillColor: dark ? const Color(0xFF111111) : Colors.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 12),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: dark ? const Color(0xFF0E1116) : Colors.white,
      indicatorColor: scheme.primaryContainer,
      surfaceTintColor: Colors.transparent,
    ),
    dividerTheme: DividerThemeData(
      color: dark ? const Color(0xFF2A2A2A) : const Color(0xFFE5E7EB),
      thickness: 1,
      space: 1,
    ),
    textTheme: const TextTheme().copyWith(
      bodyMedium: const TextStyle(fontSize: 15, height: 1.5),
    ),
  );

  return base.copyWith(
    extensions: [RumiColors.dark],
  );
}

class RumiColors extends ThemeExtension<RumiColors> {
  const RumiColors({
    required this.bubbleUser,
    required this.bubbleAssistant,
    required this.bubbleUserText,
    required this.bubbleAssistantText,
    required this.codeBackground,
    required this.accent,
  });

  final Color bubbleUser;
  final Color bubbleAssistant;
  final Color bubbleUserText;
  final Color bubbleAssistantText;
  final Color codeBackground;
  final Color accent;

  static const dark = RumiColors(
    bubbleUser: Color(0xFFE8E8EA),
    bubbleAssistant: Color(0xFF1C1C1E),
    bubbleUserText: Color(0xFF111111),
    bubbleAssistantText: Color(0xFFEDEDED),
    codeBackground: Color(0xFF0F0F10),
    accent: Color(0xFFB8B8BB),
  );

  @override
  RumiColors copyWith({
    Color? bubbleUser,
    Color? bubbleAssistant,
    Color? bubbleUserText,
    Color? bubbleAssistantText,
    Color? codeBackground,
    Color? accent,
  }) {
    return RumiColors(
      bubbleUser: bubbleUser ?? this.bubbleUser,
      bubbleAssistant: bubbleAssistant ?? this.bubbleAssistant,
      bubbleUserText: bubbleUserText ?? this.bubbleUserText,
      bubbleAssistantText: bubbleAssistantText ?? this.bubbleAssistantText,
      codeBackground: codeBackground ?? this.codeBackground,
      accent: accent ?? this.accent,
    );
  }

  @override
  RumiColors lerp(RumiColors? other, double t) {
    if (other == null) return this;
    return RumiColors(
      bubbleUser: Color.lerp(bubbleUser, other.bubbleUser, t)!,
      bubbleAssistant: Color.lerp(bubbleAssistant, other.bubbleAssistant, t)!,
      bubbleUserText: Color.lerp(bubbleUserText, other.bubbleUserText, t)!,
      bubbleAssistantText:
          Color.lerp(bubbleAssistantText, other.bubbleAssistantText, t)!,
      codeBackground: Color.lerp(codeBackground, other.codeBackground, t)!,
      accent: Color.lerp(accent, other.accent, t)!,
    );
  }
}
