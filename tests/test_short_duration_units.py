import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import av
from downloads import download_audio
from maoer_api import ApiError, MediaItem, PlaybackInfo, _duration_ms

class ShortDurationUnitTests(unittest.TestCase):
    def test_official_duration_values_are_milliseconds_even_below_ten_seconds(self):
        for value in (1,5781,6443,7279,8986,9999,10000,31857):
            with self.subTest(value=value):
                self.assertEqual(_duration_ms(str(value)),value)

    def test_download_uses_file_integrity_not_metadata_duration(self):
        data=io.BytesIO()
        with av.open(data,'w',format='mp4') as source:
            stream=source.add_stream('aac',rate=48000)
            stream.layout='mono'
            for index in range(300):
                frame=av.AudioFrame(format='fltp',layout='mono',samples=1024)
                frame.sample_rate=48000
                frame.pts=index*1024
                for plane in frame.planes:plane.update(bytes(plane.buffer_size))
                source.mux(stream.encode(frame))
            source.mux(stream.encode(None))
        payload = data.getvalue()
        api = Mock()
        item = MediaItem('sound', 1, 'fixture')
        with tempfile.TemporaryDirectory() as folder, patch('downloads.MaoerApi') as anonymous:
            response = anonymous.return_value.session.__enter__.return_value.get.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'Content-Length': str(len(payload))}
            response.iter_content.return_value = [payload]
            for duration in (3000, 6443, 12000):
                with self.subTest(metadata_duration=duration):
                    api.playback_info.return_value = PlaybackInfo(1, 'fixture', '', duration_ms=duration,
                        dash_audio={'base_url': 'https://media.invalid/audio.m4s', 'size': len(payload)})
                    saved = download_audio(api, item, Path(folder) / str(duration),
                        threading.Event(), Mock(), Mock())
                    with av.open(str(saved)) as check:
                        self.assertGreater(check.duration / 1000, 5000)
                        self.assertIsNotNone(next(check.decode(audio=0), None))
            response.iter_content.return_value = [payload[:-5]]
            truncated = Path(folder) / 'truncated'
            with self.assertRaisesRegex(ApiError, '音频下载不完整'):
                download_audio(api, item, truncated, threading.Event(), Mock(), Mock())
            self.assertEqual(list(truncated.iterdir()), [])
