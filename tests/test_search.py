"""Search request isolation regressions; no network, account or playback."""
import unittest
from unittest.mock import Mock

import wx

from app import MaoerFrame
from maoer_api import MediaItem


class SearchRequestTests(unittest.TestCase):
    def setUp(self):
        self.application = wx.GetApp() or wx.App(False)
        self.frame = MaoerFrame.__new__(MaoerFrame)
        wx.Frame.__init__(self.frame, None)
        self.frame._build_ui()
        self.addCleanup(self.frame.Destroy)
        self.frame.api = Mock()
        self.frame.api.search.return_value = [MediaItem('drama', 94733, '灯花笑 上季')]
        self.frame._set_root_items = Mock()
        self.frame.load_homepage = Mock()
        self.frame.show_error = Mock()
        self.jobs = []
        self.frame._run_background = lambda status, work, done, **kwargs: self.jobs.append((work, done, kwargs))

    def submit(self, text):
        self.frame.search_box.SetValue(text)
        event = Mock()
        event.GetEventObject.return_value = self.frame.search_box
        self.frame.on_search(event)

    def test_late_first_search_cannot_overwrite_a_newer_search(self):
        self.submit('灯华笑')
        first = self.jobs.pop()
        self.submit('错登科')
        second = self.jobs.pop()
        second[1](second[0]())
        self.frame._set_root_items.reset_mock()
        first[1](first[0]())
        self.frame._set_root_items.assert_not_called()

    def test_clear_search_returns_home_and_discards_late_results(self):
        self.submit('灯花笑')
        first = self.jobs.pop()
        self.submit('   ')
        self.frame.load_homepage.assert_called_once_with(focus_list=True)
        first[1](first[0]())
        self.frame._set_root_items.assert_not_called()

    def test_account_change_does_not_issue_old_page_queries_with_new_api(self):
        old_api = self.frame.api
        self.submit('灯花笑')
        work, done, _ = self.jobs.pop()
        done(work())
        state = self.frame._set_root_items.call_args.kwargs['page_state']
        self.frame.api = Mock()
        self.assertEqual(state.loader(2), [])
        self.frame.api.search.assert_not_called()
        self.assertEqual(old_api.search.call_count, 1)

    def test_pagination_keeps_submitted_keyword_and_only_trims_whitespace(self):
        self.submit('  灯华笑  ')
        work, done, _ = self.jobs.pop()
        done(work())
        self.frame.api.search.assert_called_once_with('灯华笑', 1)
        state = self.frame._set_root_items.call_args.kwargs['page_state']
        self.frame.search_box.SetValue('其他关键词')
        state.loader(2)
        self.frame.api.search.assert_called_with('灯华笑', 2)

    def test_empty_results_do_not_trigger_more_pages(self):
        self.frame.api.search.return_value = []
        self.submit('没有结果')
        work, done, _ = self.jobs.pop()
        done(work())
        state = self.frame._set_root_items.call_args.kwargs['page_state']
        self.assertFalse(state.has_more)

    def test_late_error_is_ignored_but_current_error_is_shown(self):
        self.submit('灯花笑')
        first = self.jobs.pop()
        self.submit('错登科')
        second = self.jobs.pop()
        first[2]['on_error']('旧搜索失败')
        self.frame.show_error.assert_not_called()
        second[2]['on_error']('当前搜索失败')
        self.frame.show_error.assert_called_once_with('当前搜索失败')


if __name__ == '__main__':
    unittest.main()
