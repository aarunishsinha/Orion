import os
import re
import logging
import tempfile
import subprocess

logger = logging.getLogger("tts_engine")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TTS_VOICE = os.environ.get("ORION_TTS_VOICE", "Ava (Premium)")
TTS_RATE = int(os.environ.get("ORION_TTS_RATE", "190"))  # words per minute


class TTSEngine:
    """
    macOS-native Text-to-Speech engine using the built-in `say` and `afplay`
    commands. Generates audio to a temp file, measures its duration via
    `afinfo`, and plays it back — enabling the caller to sync a typing
    animation to the exact speech duration.
    """

    def __init__(self, voice: str = TTS_VOICE, rate: int = TTS_RATE):
        self.voice = voice
        self.rate = rate
        self._current_playback: subprocess.Popen | None = None
        logger.info("TTS engine initialized (voice=%s, rate=%d wpm)", voice, rate)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def speak(self, text: str) -> tuple[float, subprocess.Popen]:
        """
        Generate speech audio for `text` and begin playback.

        Returns:
            (duration_seconds, playback_process)

            - duration_seconds: exact length of the audio in seconds
            - playback_process: the running `afplay` subprocess — call
              `.wait()` to block until playback completes, or `.terminate()`
              to cancel early.
        """
        # 1. Generate audio to temp file
        tmp_path = self._generate_audio(text)

        # 2. Measure duration
        duration = self._get_duration(tmp_path)
        logger.info("TTS generated: %.2fs for %d chars (voice=%s)",
                     duration, len(text), self.voice)

        # 3. Start non-blocking playback
        playback = self._play_audio(tmp_path)

        return duration, playback

    def cancel(self):
        """Stop any in-progress playback immediately."""
        if self._current_playback and self._current_playback.poll() is None:
            self._current_playback.terminate()
            self._current_playback.wait(timeout=2)
            logger.info("TTS playback cancelled")
        self._current_playback = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _generate_audio(self, text: str) -> str:
        """
        Run macOS `say` to generate an AIFF audio file from text.
        Returns the path to the temporary audio file.
        """
        tmp = tempfile.NamedTemporaryFile(
            suffix=".aiff", prefix="orion_tts_", delete=False
        )
        tmp_path = tmp.name
        tmp.close()

        subprocess.run(
            ["say", "-v", self.voice, "-r", str(self.rate), "-o", tmp_path, text],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return tmp_path

    def _get_duration(self, audio_path: str) -> float:
        """
        Parse audio duration in seconds from macOS `afinfo` output.
        Falls back to a rough estimate if parsing fails.
        """
        try:
            result = subprocess.run(
                ["afinfo", audio_path],
                capture_output=True, text=True, timeout=5,
            )
            match = re.search(r"estimated duration:\s*([\d.]+)", result.stdout)
            if match:
                return float(match.group(1))
        except Exception as e:
            logger.warning("afinfo failed, estimating duration: %s", e)

        # Rough fallback: ~2.5 words/sec at default rate
        word_count = len(audio_path.split())
        return max(1.0, word_count / 2.5)

    def _play_audio(self, audio_path: str) -> subprocess.Popen:
        """
        Start `afplay` as a background process. The temp file is cleaned up
        via a wrapper that waits for playback to finish.
        """
        self.cancel()  # stop any existing playback

        proc = subprocess.Popen(
            ["afplay", audio_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._current_playback = proc

        # Schedule cleanup of the temp file after playback finishes
        import threading
        def _cleanup():
            proc.wait()
            try:
                os.unlink(audio_path)
            except OSError:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()

        return proc
