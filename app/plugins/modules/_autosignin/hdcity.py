from app.plugins.modules._autosignin._base import _ISiteSigninHandler
from app.utils import SiteUtils, RequestUtils
from config import Config


class HDCity(_ISiteSigninHandler):
    """
    城市签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "hdcity.city"

    # 签到成功
    _success_text = '本次签到获得魅力'
    # 重复签到
    _repeat_text = '已签到'

    @classmethod
    def match(cls, url):
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if SiteUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: dict):
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxy = Config().get_proxies() if site_info.get("proxy") else None

        # 获取页面html
        html_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=proxy
                                ).get_res(url="https://hdcity.city/sign")
        if not html_res or html_res.status_code != 200:
            self.error(f"签到失败，请检查站点连通性")
            return False, f'【{site}】签到失败，请检查站点连通性'

        if "login" in html_res.text:
            self.error(f"签到失败，cookie失效")
            return False, f'【{site}】签到失败，cookie失效'

        # 判断是否已签到
        # '已连续签到278天，此次签到您获得了100魔力值奖励!'
        if self._success_text in html_res.text:
            self.info(f"签到成功")
            return True, f'【{site}】签到成功'
        if self._repeat_text in html_res.text:
            self.info(f"今日已签到")
            return True, f'【{site}】今日已签到'
        self.error(f"签到失败，签到接口返回 {html_res.text}")
        return False, f'【{site}】签到失败'
