import unittest

from maoer_api import DRAMA_PAY_TYPE_WHOLE, MaoerApi


class HomepagePurchasedDramasTest(unittest.TestCase):
    def test_homepage_marks_full_purchased_drama_recommendations(self) -> None:
        api = MaoerApi(cookie="token=abc")
        calls: list[tuple[str, dict[str, object] | None]] = []

        def fake_get(path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            calls.append((path, params))
            if path == "/site/homepage":
                return {"info": {}}
            if path == "/dramaapi/summerdrama":
                return {
                    "info": [
                        [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                                "need_pay": 1,
                            }
                        ]
                    ]
                }
            if path == "/reward/drama-reward-rank":
                return {"info": {"ranks": {"Datas": []}}}
            if path == "/mperson/getdramabought":
                return {
                    "info": {
                        "data": [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                            }
                        ]
                    }
                }
            raise AssertionError(f"unexpected path: {path}")

        api._open_homepage_shell = lambda: None  # type: ignore[method-assign]
        api._get = fake_get  # type: ignore[method-assign]

        items = api.homepage()

        self.assertEqual(1, len(items))
        self.assertEqual("drama", items[0].kind)
        self.assertEqual(123, items[0].id)
        self.assertTrue(items[0].raw.get("_purchased_full_drama"))
        self.assertEqual(
            ["/mperson/getdramabought"],
            [path for path, _params in calls if path == "/mperson/getdramabought"],
        )

    def test_buy_drama_clears_cached_homepage_purchase_state(self) -> None:
        api = MaoerApi(cookie="token=abc")
        api._purchased_full_drama_ids_cache = set()
        posted: list[tuple[str, dict[str, str], str | None]] = []

        def fake_post_form_api(
            path: str,
            data: dict[str, str],
            referer: str | None = None,
        ) -> dict[str, object]:
            posted.append((path, data, referer))
            return {"success": True}

        api._post_form_api = fake_post_form_api  # type: ignore[method-assign]

        api.buy_drama(123)

        self.assertIsNone(api._purchased_full_drama_ids_cache)
        self.assertEqual(
            [("/financial/buydrama", {"drama_id": "123"}, "https://www.missevan.com/mdrama/123")],
            posted,
        )

    def test_search_marks_full_purchased_drama_results(self) -> None:
        api = MaoerApi(cookie="token=abc")

        def fake_get(path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            if path == "/dramaapi/search":
                return {
                    "info": {
                        "Datas": [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                                "need_pay": 1,
                            }
                        ]
                    }
                }
            if path == "/sound/getsearch":
                return {"info": {"Datas": []}}
            if path == "/mperson/getdramabought":
                return {
                    "info": {
                        "data": [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                            }
                        ]
                    }
                }
            raise AssertionError(f"unexpected path: {path}")

        api._get = fake_get  # type: ignore[method-assign]

        items = api.search("Bought")

        self.assertEqual(1, len(items))
        self.assertTrue(items[0].raw.get("_purchased_full_drama"))

    def test_subscriptions_mark_full_purchased_dramas(self) -> None:
        api = MaoerApi(cookie="token=abc")

        def fake_get(path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            if path == "/dramaapi/getusersubscriptions":
                return {
                    "info": {
                        "Datas": [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                                "need_pay": 1,
                            }
                        ]
                    }
                }
            if path == "/mperson/getdramabought":
                return {
                    "info": {
                        "data": [
                            {
                                "id": 123,
                                "name": "Bought Drama",
                                "pay_type": DRAMA_PAY_TYPE_WHOLE,
                            }
                        ]
                    }
                }
            raise AssertionError(f"unexpected path: {path}")

        api._get = fake_get  # type: ignore[method-assign]

        items = api.user_subscribed_dramas(456)

        self.assertEqual(1, len(items))
        self.assertTrue(items[0].raw.get("_purchased_full_drama"))


if __name__ == "__main__":
    unittest.main()
