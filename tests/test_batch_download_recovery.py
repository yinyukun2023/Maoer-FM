import threading
import unittest
from unittest.mock import Mock, patch

from browser_player import HiddenBrowserPlayer
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired
from download_dialog import DownloadPartResult, DownloadProgressDialog, DownloadRequest, _DownloadPlayer
from downloads import DownloadCancelled
from tests.test_download_controls import DownloadControlTests


class DownloadRequestReductionTests(unittest.TestCase):
    def test_same_episode_metadata_is_reused_only_inside_one_download_job(self):
        api = MaoerApi(cookie='test=keep', cache_sound_metadata=True)
        self.addCleanup(api.session.close)
        item = MediaItem('sound', 1, '集')
        with patch.object(api, '_get', return_value={'info': {'sound': {'need_pay':0}}}) as get:
            api.playback_info(item)
            api.playback_info(item)
        self.assertEqual(get.call_count, 1)
        other = MaoerApi(cookie='test=keep', cache_sound_metadata=True)
        self.addCleanup(other.session.close)
        with patch.object(other, '_get', return_value={'info': {'sound': {'need_pay':0}}}) as get:
            other.playback_info(item)
        self.assertEqual(get.call_count, 1)

    def test_normal_playback_is_not_cached(self):
        api = MaoerApi(cookie='test=keep')
        self.addCleanup(api.session.close)
        with patch.object(api, '_get', return_value={'info': {'sound': {'need_pay':0}}}) as get:
            api.playback_info(MediaItem('sound', 1, '集'))
            api.playback_info(MediaItem('sound', 1, '集'))
        self.assertEqual(get.call_count, 2)

    def test_download_player_does_not_reload_home_for_each_episode(self):
        player = HiddenBrowserPlayer(None, cookie='test=keep', reuse_cookie_primer=True)
        view = Mock()
        player._webview = view
        player._run_control_callback=Mock(side_effect=lambda a,v,cb:cb({'ok':False}))
        with patch.object(player, '_ensure_webview', return_value=view), patch.object(player, '_install_user_scripts'), patch('browser_player.wx.CallLater'):
            player.play(PlaybackInfo(1, '一', ''))
            player._load_cookie_primer_target(player._load_generation)
            for sound_id in range(2,32):
                player.play(PlaybackInfo(sound_id, '集', ''))
        urls = [call.args[0] for call in view.LoadURL.call_args_list]
        self.assertEqual(urls.count('https://www.missevan.com/'), 1)
        self.assertTrue(urls[-1].endswith('id=31'))

    def test_cached_metadata_still_checks_purchase_for_each_item(self):
        api=MaoerApi(cookie='test=keep',cache_sound_metadata=True)
        self.addCleanup(api.session.close)
        owned=MediaItem('sound',1,'集',raw={'_purchased_sound':True})
        with patch.object(api,'_get',return_value={'info':{'sound':{'need_pay':1}}}) as get:
            api.playback_info(owned)
            with self.assertRaises(PurchaseRequired):
                api.playback_info(MediaItem('sound',1,'集'))
        self.assertEqual(get.call_count,1)

    def test_transient_metadata_failure_can_outlast_first_three_checks(self):
        api = MaoerApi(cookie='test=keep')
        self.addCleanup(api.session.close)
        with patch.object(api, '_get', side_effect=[ApiError('该音频不存在')]*3 + [{'info':{'sound':{'need_pay':0}}}]), patch('maoer_api.time.sleep'):
            self.assertEqual(api.playback_info(MediaItem('sound', 1, '集')).sound_id, 1)


class PreparationCallbackTests(DownloadControlTests):
    def test_download_rejection_does_not_trigger_a_fast_inner_reload(self):
        player = _DownloadPlayer.__new__(_DownloadPlayer)
        HiddenBrowserPlayer.__init__(player, None)
        player._current = PlaybackInfo(1, '集', '')
        player._webview = Mock()
        # A navigation failure may already have queued a page reload.
        player._startup_retry_pending = True
        player._run_control_callback = Mock(side_effect=lambda action, value, done:
            done({'ok': False, 'error': 'page-rejected'}))
        with patch('browser_player.wx.CallLater') as later:
            player._autoplay_step(0, 12)
        self.assertIn('412', player._startup_error)
        later.assert_not_called()
        player._retry_startup_page(0)
        player._webview.LoadURL.assert_not_called()

    def test_only_the_412_retry_waits_30_seconds_and_can_be_cancelled(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                dialog = self.dialog()
                dialog.cancel.active = 0
                control = dialog.controls[0]
                now, calls = [100.0], []
                def request(playback, ready, result, control):
                    calls.append(now[0])
                    result['error'] = '官网播放页请求被拒绝（412），请稍后重试'
                    ready.set()
                def wait(seconds):
                    self.assertLessEqual(seconds, 0.1)
                    if cancel:
                        control.set()
                    else:
                        now[0] += 30.0
                with patch('download_dialog.time.monotonic', side_effect=lambda: now[0]), \
                        patch.object(control, 'wait', side_effect=wait) as waiting, \
                        patch.object(dialog, '_request_key', side_effect=request), \
                        patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a: f(*a)):
                    with self.assertRaises(DownloadCancelled if cancel else ApiError):
                        dialog._get_key(PlaybackInfo(1, '集', ''), control)
                    self.assertEqual(calls, [100.0] if cancel else [100.0, 130.0, 160.0])
                    self.assertEqual(waiting.call_count, 1 if cancel else 2)
                if not cancel:
                    dialog.running = True
                    dialog.player = Mock()
                    dialog._request_key(PlaybackInfo(2, '下一集', ''), threading.Event(), {}, dialog.controls[1])
                    dialog.player.play.assert_called_once()

    @patch('download_dialog.DOWNLOAD_REJECTION_COOLDOWN', 0)
    def test_transient_preparation_recreates_once_but_other_errors_do_not_retry(self):
        for message,expected in (('官网播放器未能开始加载音频，请稍后重试',2),('官网播放页请求被拒绝（412），请稍后重试',2),('无法启动音频组件',1)):
            dialog=self.dialog()
            dialog.cancel.active=0
            calls=[]
            def request(playback,ready,result,control):
                calls.append(bool(result.get('_fresh')))
                if len(calls)==1:result['error']=message
                else:result['key']=b'fixture'
                ready.set()
            with patch.object(dialog,'_request_key',side_effect=request),patch('download_dialog.wx.CallAfter',side_effect=lambda f,*a:f(*a)):
                if expected==2:
                    self.assertEqual(dialog._get_key(PlaybackInfo(1,'集',''),dialog.controls[0]),b'fixture')
                else:
                    with self.assertRaises(ApiError):dialog._get_key(PlaybackInfo(1,'集',''),dialog.controls[0])
            self.assertEqual(calls,[False,True] if expected==2 else [False])

    def test_preparation_retry_is_bounded_to_three_attempts(self):
        dialog=self.dialog()
        dialog.cancel.active=0
        calls=[]
        def request(playback,ready,result,control):
            calls.append(1)
            result['error']='官网音频准备超时，请稍后重试'
            ready.set()
        with patch.object(dialog,'_request_key',side_effect=request),patch('download_dialog.wx.CallAfter',side_effect=lambda f,*a:f(*a)):
            with self.assertRaises(ApiError):dialog._get_key(PlaybackInfo(1,'集',''),dialog.controls[0])
        self.assertEqual(len(calls),3)

    def test_third_preparation_attempt_can_succeed(self):
        dialog = self.dialog()
        dialog.cancel.active = 0
        calls = []
        def request(playback, ready, result, control):
            calls.append(bool(result.get('_fresh')))
            if len(calls) < 3:
                result['error'] = '官网音频准备超时，请稍后重试'
            else:
                result['key'] = b'fixture'
            ready.set()
        with patch.object(dialog, '_request_key', side_effect=request), \
                patch.object(dialog, '_task_progress') as progress, \
                patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a: f(*a)):
            self.assertEqual(dialog._get_key(PlaybackInfo(1, '集', ''), dialog.controls[0]), b'fixture')
        self.assertEqual(calls, [False, True, True])
        self.assertIn('第3次，共3次', progress.call_args.args[1])

    def test_31_episode_both_content_batch_uses_only_31_metadata_reads(self):
        folder=self.root/'batch31'
        dialog=DownloadProgressDialog(None,DownloadRequest('批量', [MediaItem('sound',i,'第'+str(i)+'集') for i in range(1,32)],folder,'',download_subtitles=True))
        self.addCleanup(dialog.Destroy)
        self.addCleanup(dialog.timer.Stop)
        dialog.running=dialog._started=True
        reads=[]
        def get(api,path,params=None):
            self.assertEqual(path,'/sound/getsound')
            reads.append(params['soundid'])
            return {'info':{'sound':{'need_pay':0}}}
        def save(extension):
            def part(api,item,folder,*args,**kwargs):
                api.playback_info(item)
                folder.mkdir(parents=True,exist_ok=True)
                result=folder/(str(item.id)+extension)
                result.write_bytes(b'fixture')
                return result
            return part
        callbacks=[]
        with patch.object(MaoerApi,'_get',new=get),patch('download_dialog.download_audio',side_effect=save('.mp3')),patch('download_dialog.download_subtitle',side_effect=save('.srt')),patch('download_dialog.wx.CallAfter',side_effect=lambda f,*a,**kw:callbacks.append((f,a,kw))):
            dialog._run()
        for f,a,kw in callbacks: f(*a,**kw)
        self.assertEqual(reads,list(range(1,32)))
        self.assertTrue(all(t.parts['audio'].state=='complete' and t.parts['subtitle'].state=='complete' for t in dialog.tasks))
        self.assertEqual(len(dialog._created_files),62)

    def test_all_start_only_retries_content_from_the_selected_failure_list(self):
        for kind in ('audio','subtitle'):
            with self.subTest(kind=kind):
                dialog = self.dialog(self.root / kind)
                dialog.request.download_subtitles = True
                for task, control in zip(dialog.tasks, dialog.controls):
                    task.state = 'failed'
                    task.parts = {part: DownloadPartResult('failed', part+' old failure') for part in ('audio','subtitle')}
                    control.done = control.started = True
                dialog._refresh_lists()
                listing = dialog.audio_failed_list if kind=='audio' else dialog.subtitle_failed_list
                with patch.object(listing, 'GetPopupMenuSelectionFromUser', side_effect=lambda m,p: m.GetMenuItems()[1].GetId()), patch.object(dialog, 'start'):
                    dialog._show_failed_menu(control=listing)
                calls = []
                def saved(part):
                    def save(api,item,folder,*args,**kwargs):
                        calls.append((part,item.id))
                        folder.mkdir(parents=True,exist_ok=True)
                        path = folder / (str(item.id)+('.srt' if part=='subtitle' else '.mp3'))
                        path.write_bytes(b'owned fixture')
                        return path
                    return save
                callbacks=[]
                dialog.running=True
                with patch('download_dialog.MaoerApi'), patch('download_dialog.download_audio', side_effect=saved('audio')), patch('download_dialog.download_subtitle',side_effect=saved('subtitle')), patch('download_dialog.wx.CallAfter',side_effect=lambda f,*a,**kw:callbacks.append((f,a,kw))):
                    dialog._run()
                for f,a,kw in callbacks: f(*a,**kw)
                self.assertEqual(calls,[(kind,1),(kind,2),(kind,3)])
                other = 'subtitle' if kind=='audio' else 'audio'
                self.assertTrue(all(t.parts[kind].state=='complete' and t.parts[other].reason==other+' old failure' for t in dialog.tasks))

    def test_inline_preparation_callback_does_not_lock_pending_forever(self):
        dialog = self.dialog()
        dialog.running = True
        playback = PlaybackInfo(1, '集', '', dash_audio={'bilidrm_uri':'uri:bili://'+'00'*16})
        dialog.key_request = (playback, threading.Event(), {})
        dialog.key_control = dialog.controls[0]
        dialog.key_started = dialog.cancel.active_time()
        dialog.player = Mock()
        dialog.player._startup_error = ''
        def inline(view, method, params, received):
            received(0, '{"result":{"value":null}}')
            return Mock()
        with patch('download_dialog.call_webview_devtools', side_effect=inline):
            dialog._poll_key(None)
        self.assertFalse(dialog.key_pending, 'inline callback was overwritten by pending=True')

    def test_explicit_page_failure_wakes_preparation_instead_of_waiting_45_seconds(self):
        dialog = self.dialog()
        dialog.running = True
        ready, result = threading.Event(), {}
        dialog.key_request = (PlaybackInfo(1, '集', ''), ready, result)
        dialog.key_control = dialog.controls[0]
        dialog.key_started = dialog.cancel.active_time()
        dialog.player = Mock()
        dialog.player._startup_error = '官网播放页加载失败（412），请稍后重试'
        dialog._poll_key(None)
        self.assertTrue(ready.is_set())
        self.assertIn('412', result.get('error',''))
