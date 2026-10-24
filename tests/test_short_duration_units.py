import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import av
from downloads import remux_audio
from maoer_api import ApiError,MaoerApi,MediaItem,_duration_ms

class ShortDurationUnitTests(unittest.TestCase):
    def test_official_duration_values_are_milliseconds_even_below_ten_seconds(self):
        for value in (1,5781,6443,7279,8986,9999,10000,31857):
            with self.subTest(value=value):
                self.assertEqual(_duration_ms(str(value)),value)

    def test_complete_short_audio_passes_but_a_truncated_one_still_fails(self):
        api=MaoerApi(cookie='')
        self.addCleanup(api.session.close)
        with patch.object(api,'_get',return_value={'info':{'sound':{'duration':'6443','need_pay':0}}}):
            playback=api.playback_info(MediaItem('sound',3055563,'起床铃声-陈启明'))
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
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'short.m4a'
            remux_audio(io.BytesIO(data.getvalue()),path,None,threading.Event(),playback.duration_ms)
            self.assertTrue(path.exists())
            with self.assertRaisesRegex(ApiError,'时长不完整'):
                remux_audio(io.BytesIO(data.getvalue()),Path(folder)/'truncated.m4a',None,threading.Event(),12000)
