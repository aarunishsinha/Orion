import logging
import queue
import wave
import tempfile
import subprocess
import pyaudio

logger = logging.getLogger("audio_engine")

class AudioEngine:
    def __init__(self, display_queue: queue.Queue):
        self.display_queue = display_queue
        self.is_recording = False
        
        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 16000
        
        self.p = pyaudio.PyAudio()
        self.audio_queue = queue.Queue()
        self.stream = None
        
        # Homebrew whisper-cli (pre-compiled native Intel binary)
        self.whisper_bin = "/usr/local/bin/whisper-cli"
        self.model_path = "/Users/aarunishsinha/Library/Application Support/pywhispercpp/models/ggml-base.en.bin"
        
        logger.info("Initialized Native Homebrew STT (whisper-cli, CPU-only)...")

    def start_recording(self):
        if self.is_recording:
            return
            
        self.is_recording = True
        self.audio_queue.queue.clear()
        
        self.stream = self.p.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            frames_per_buffer=self.CHUNK,
            stream_callback=self._audio_callback
        )
        self.stream.start_stream()
        logger.info("Microphone stream started.")
        
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Called by PyAudio for each chunk of incoming mic data."""
        if self.is_recording:
            self.audio_queue.put(in_data)
            return (None, pyaudio.paContinue)
        return (None, pyaudio.paComplete)

    def stop_recording_and_transcribe(self) -> str:
        """Stops mic and runs Whisper transcription via a seekable temp file."""
        self.is_recording = False
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            
        logger.info("Microphone stream closed.")
        
        frames = []
        while not self.audio_queue.empty():
            frames.append(self.audio_queue.get_nowait())
            
        if not frames:
            return ""

        raw_data = b"".join(frames)
        
        # Write to a seekable temp file (auto-deleted when closed)
        # whisper-cli requires seek() on WAV headers — pipes don't support this
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self.CHANNELS)
                wf.setsampwidth(self.p.get_sample_size(self.FORMAT))
                wf.setframerate(self.RATE)
                wf.writeframes(raw_data)
            
            # -ng = CPU-only (bypasses AMD Radeon Metal float16 corruption)
            # -l en = force English
            logger.info("Transcribing via whisper-cli (CPU-only)...")
            try:
                result = subprocess.run(
                    [
                        self.whisper_bin,
                        "-m", self.model_path,
                        "-f", tmp.name,
                        "-l", "en",
                        "-ng"
                    ],
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                # Parse timestamped lines: [00:00:00.000 --> 00:00:04.000]  Hello
                lines = result.stdout.strip().split('\n')
                text_parts = []
                for line in lines:
                    line = line.strip()
                    if line.startswith('[') and ']' in line:
                        text_parts.append(line.split(']', 1)[1].strip())
                    elif line:
                        text_parts.append(line)
                text = " ".join(text_parts).strip()
                
            except subprocess.CalledProcessError as e:
                text = ""
                logger.error(f"whisper-cli error: {e.stderr}")
            except Exception as e:
                text = ""
                logger.error(f"STT Error: {e}")
            
        logger.info(f"Transcription complete: {text}")
        return text

    def kill_all(self):
        self.is_recording = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()

