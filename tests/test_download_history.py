import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from wx import html2

from browser_player import HiddenBrowserPlayer
from download_dialog import DOWNLOAD_NO_HISTORY_SCRIPT, _DownloadPlayer


@unittest.skipUnless(shutil.which('node'), 'Node.js executes the download history script')
class DownloadHistoryTests(unittest.TestCase):
    def run_script(self, test):
        fixture = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const sent = [];
class XHR {
  open(...args) { this.openArgs = args; }
  send(body) { sent.push({transport: 'xhr', args: this.openArgs, body}); }
}
const context = {
  URL, URLSearchParams, Promise, Response, Request, Headers, WeakSet,
  location: {href: 'https://www.missevan.com/sound/player?id=1'},
  fetch(...args) { sent.push({transport: 'fetch', args}); return Promise.resolve('native'); },
  navigator: {sendBeacon(...args) {sent.push({transport: 'beacon', args}); return false;}},
  XMLHttpRequest: XHR,
};
context.window = context;
vm.createContext(context);
'''
        script = fixture + 'vm.runInContext(' + json.dumps(DOWNLOAD_NO_HISTORY_SCRIPT) + ', context);\n'
        script += '(async () => {\n' + test + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
        result = subprocess.run([shutil.which('node'), '-'], input=script, text=True,
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fetch_reports_are_local_even_for_request_and_url_objects(self):
        self.run_script(r'''
for (const input of [
  '/sound/addplaytimes?sound_id=1&drama_id=2',
  new URL('https://www.missevan.com/sound/addplaytimes?sound_id=1'),
  new Request('https://data.missevan.com/statistics/playlog-web?sound_id=1&v=2', {method: 'POST'})
]) {
  const response = await context.fetch(input, {method: 'POST', body: 'position=9'});
  assert.equal(response.status, 200);
  assert.equal((await response.json()).success, true);
}
assert.equal(sent.length, 0);
''')

    def test_beacon_reports_do_not_send_when_stopping_or_closing(self):
        self.run_script(r'''
assert.equal(context.navigator.sendBeacon('https://data.missevan.com/statistics/playlog-web?sound_id=1', 'operation_type=4'), true);
assert.equal(context.navigator.sendBeacon('/sound/addplaytimes?sound_id=1'), true);
assert.equal(sent.length, 0);
assert.equal(context.navigator.sendBeacon('https://data.missevan.com/unrelated', 'keep'), false);
assert.equal(sent.length, 1);
assert.deepEqual(sent[0].args, ['https://data.missevan.com/unrelated', 'keep']);
''')

    def test_xhr_reports_use_native_local_response_and_reuse_is_unchanged(self):
        self.run_script(r'''
const request = new context.XMLHttpRequest();
request.open('POST', '/sound/addplaytimes?sound_id=1', true);
request.send('do-not-send');
assert.equal(sent[0].args[0], 'GET');
assert.ok(sent[0].args[1].startsWith('data:application/json,'));
assert.equal(JSON.parse(decodeURIComponent(sent[0].args[1].split(',')[1])).success, true);
assert.equal(sent[0].body, null);
request.open('POST', 'https://data.missevan.com/statistics/playlog-web?sound_id=1', false);
request.send('do-not-send');
assert.equal(sent[1].args[2], false);
request.open('POST', '/sound/getsound', true, 'user', 'password');
request.send('original-body');
assert.deepEqual(sent[2].args, ['POST', '/sound/getsound', true, 'user', 'password']);
assert.equal(sent[2].body, 'original-body');
''')

    def test_permissions_media_and_unrelated_hosts_pass_through(self):
        self.run_script(r'''
for (const url of [
  '/sound/getsound?soundid=1', '/x/vip/subscribe-info', '/account/status',
  'https://media.example/audio.m4a', '/sound/getsound?next=/sound/addplaytimes',
  'https://missevan.com.example/sound/addplaytimes',
  'https://other.example/statistics/playlog-web',
  '/sound/addplaytimes-extra', 'https://www.missevan.com/mperson/gethistory'
]) {
  assert.equal(await context.fetch(url, {method: 'POST', body: 'keep'}), 'native');
  assert.equal(sent.at(-1).args[0], url);
  assert.equal(sent.at(-1).args[1].body, 'keep');
}
''')

    def test_double_install_is_idempotent(self):
        self.run_script('const first = context.fetch;\nvm.runInContext(' +
                        json.dumps(DOWNLOAD_NO_HISTORY_SCRIPT) + ', context);\n' +
                        'assert.equal(context.fetch, first);')

    def test_only_download_player_installs_filter_at_document_start(self):
        normal = HiddenBrowserPlayer(None, cookie='')
        downloader = _DownloadPlayer.__new__(_DownloadPlayer)
        HiddenBrowserPlayer.__init__(downloader, None, cookie='')
        for player, expected in ((normal, False), (downloader, True)):
            with self.subTest(download=expected):
                view = Mock()
                player._install_user_scripts(view)
                matches = [call for call in view.AddUserScript.call_args_list
                           if call.args[0] == DOWNLOAD_NO_HISTORY_SCRIPT]
                self.assertEqual(len(matches), int(expected))
                if expected:
                    self.assertEqual(matches[0].args[1], html2.WEBVIEW_INJECT_AT_DOCUMENT_START)


if __name__ == '__main__':
    unittest.main()
