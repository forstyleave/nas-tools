
import asyncio
import base64
import datetime
import hashlib
import json
import mimetypes
import os.path
import re
import threading
import traceback
import urllib
import xml.dom.minidom

from math import floor
from pathlib import Path
from threading import Lock
from urllib.parse import unquote

from fastapi import Body, FastAPI, File, Request, Response, UploadFile, WebSocket, HTTPException, status
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from icalendar import Calendar, Event, Alarm
from simple_websocket import ConnectionClosed
from sse_starlette.sse import EventSourceResponse 

import log
from app.brushtask import BrushTask
from app.conf import ModuleConf, SystemConfig
from app.downloader import Downloader
from app.filter import Filter
from app.helper import SecurityHelper, MetaHelper, ThreadHelper
from app.indexer import Indexer
from app.media.meta import MetaInfo
from app.mediaserver import MediaServer
from app.message import Message
from app.plugins import EventManager
from app.rsschecker import RssChecker
from app.sites import Sites, SiteUserInfo
from app.subscribe import Subscribe
from app.sync import Sync
from app.torrentremover import TorrentRemover
from app.utils import DomUtils, SystemUtils, ExceptionUtils, StringUtils
from app.utils.types import *
from config import PT_TRANSFER_INTERVAL, Config, TMDB_API_DOMAINS
from web.action import WebAction

from web.auth import current_user, login_required, try_login
from web.backend.WXBizMsgCrypt3 import WXBizMsgCrypt
from web.backend.wallpaper import get_login_wallpaper
from web.backend.web_utils import WebUtils
from web.security import require_auth

# 配置文件锁
ConfigLock = Lock()

App = FastAPI(title='nastool')

templates = Jinja2Templates(directory="web/templates")
App.mount("/static", StaticFiles(directory="web/static"), name="static")
# 添加 CORS 中间件，允许所有来源
App.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SSE
LoggingLock = Lock()
LoggingSource = ""

# fix Windows registry stuff
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')


# 自定义处理错误
@App.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "404.html",
            {"request": request, "error": str(exc.detail)},
            status_code=404
        )
    if exc.status_code == 500:
        return templates.TemplateResponse(
            "404.html",
            {"request": request, "error": str(exc.detail)},
            status_code=500
        )
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@App.middleware("http")
async def disable_buffering(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Accel-Buffering"] = "no"  # 禁用缓冲
    return response

def make_response(html_content, status_code=200):
    return HTMLResponse(content=html_content, status_code=status_code)

# 主页面
@App.api_route('/', methods=['GET', 'POST'])
async def login(request: Request):

    def redirect_to_navigation():
        """
        跳转到导航页面
        """
        # 让当前用户生效
        MediaServer().init_config()
        # 跳转页面
        if GoPage and GoPage != 'web':
            return RedirectResponse(url='/web#' + GoPage, status_code=302)
        else:
            return RedirectResponse(url='/web', status_code=302)

    def redirect_to_login(request: Request, errmsg=''):
        """
        跳转到登录页面
        """
        image_code, img_title, img_link = get_login_wallpaper()
        response = {
            "request": request,
            "GoPage": GoPage,
            "image_code": image_code,
            "img_title": img_title,
            "img_link": img_link,
            "err_msg": errmsg
        }
        return templates.TemplateResponse('login.html', response)

    # 登录认证
    if request.method == 'GET':
        GoPage = request.query_params.get("next") or ""
        if GoPage.startswith('/'):
            GoPage = GoPage[1:]
        
        if current_user:
            return redirect_to_navigation()
        else:
            return redirect_to_login(request)

    else:
        form_data = await request.form()
        if not form_data:
            return redirect_to_login(request, '请输入用户名')

        GoPage = form_data.get('next') or ""
        if GoPage.startswith('/'):
            GoPage = GoPage[1:]
        username = form_data.get('username')
        password = form_data.get('password')
        remember = form_data.get('remember')
        if not username:
            return redirect_to_login(request, '请输入用户名')
        
        access_token = try_login(username, password)
        if not access_token:
            return redirect_to_login(request, '用户名或密码错误')
        
        redirect_response = redirect_to_navigation()
        # 设置 HTTP-Only Cookie
        redirect_response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
        return redirect_response


# 退出登录路由
@App.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key="access_token")
    return { "code":0, "message": "Logout successful"}


@App.api_route('/web', methods=['POST', 'GET'])
@login_required
def web(request: Request):
    # 跳转页面
    GoPage = request.query_params.get("next") or ""
    # 判断当前的运营环境
    SystemFlag = SystemUtils.get_system()
    SyncMod = Config().get_config('media').get('default_rmt_mode')
    TMDBFlag = 1 if Config().get_config('app').get('rmt_tmdbkey') else 0
    DefaultPath = Config().get_config('media').get('media_default_path')
    if not SyncMod:
        SyncMod = "link"
    RmtModeDict = WebAction().get_rmt_modes()
    RestypeDict = ModuleConf.TORRENT_SEARCH_PARAMS.get("restype")
    PixDict = ModuleConf.TORRENT_SEARCH_PARAMS.get("pix")
    SiteFavicons = Sites().get_site_favicon()
    Indexers = Indexer().get_indexers()
    SearchSource = "douban" if Config().get_config("laboratory").get("use_douban_titles") else "tmdb"
    CustomScriptCfg = SystemConfig().get(SystemConfigKey.CustomScript)
    Menus = WebAction().get_user_menus().get("menus") or []
    Commands = WebAction().get_commands()

    response = {
        "request": request,  
        "GoPage": GoPage,
        "CurrentUser": current_user,
        "SystemFlag": SystemFlag.value,
        "TMDBFlag": TMDBFlag,
        "AppVersion": WebUtils.get_current_version(),
        "RestypeDict": RestypeDict,
        "PixDict": PixDict,
        "SyncMod": SyncMod,
        "SiteFavicons": SiteFavicons,
        "RmtModeDict": RmtModeDict,
        "Indexers": Indexers,
        "SearchSource": SearchSource,
        "CustomScriptCfg": CustomScriptCfg,
        "DefaultPath": DefaultPath,
        "Menus": Menus,
        "Commands": Commands
        }

    return templates.TemplateResponse("navigation.html", response)


# 开始
@App.api_route('/index', methods=['POST', 'GET'])
@login_required
def index(request: Request):
    # 媒体服务器类型
    MSType = Config().get_config('media').get('media_server')
    # 获取媒体数量
    MediaCounts = WebAction().get_library_mediacount()
    if MediaCounts.get("code") == 0:
        ServerSucess = True
    else:
        ServerSucess = False

    # 获得活动日志
    Activity = WebAction().get_library_playhistory().get("result")

    # 磁盘空间
    LibrarySpaces = WebAction().get_library_spacesize()

    # 媒体库
    Librarys = MediaServer().get_libraries()
    LibrarySyncConf = SystemConfig().get(SystemConfigKey.SyncLibrary) or []

    # 继续观看
    Resumes = MediaServer().get_resume()

    # 最近添加
    Latests = MediaServer().get_latest()

    response = {
        "request": request,  
        "ServerSucess": ServerSucess,
        "MediaCount": {'MovieCount': MediaCounts.get("Movie"),
                    'SeriesCount': MediaCounts.get("Series"),
                    'SongCount': MediaCounts.get("Music"),
                    "EpisodeCount": MediaCounts.get("Episodes")},
        "Activitys": Activity,
        "UserCount": MediaCounts.get("User"),
        "FreeSpace": LibrarySpaces.get("FreeSpace"),
        "TotalSpace": LibrarySpaces.get("TotalSpace"),
        "UsedSapce": LibrarySpaces.get("UsedSapce"),
        "UsedPercent": LibrarySpaces.get("UsedPercent"),
        "MediaServerType": MSType,
        "Librarys": Librarys,
        "LibrarySyncConf": LibrarySyncConf,
        "Resumes": Resumes,
        "Latests": Latests
        }

    return templates.TemplateResponse("index.html", response)


# 资源搜索页面
@App.api_route('/search', methods=['POST', 'GET'])
@login_required
def search(request: Request):
    # 权限
    pris = current_user.pris
    # 结果
    res = WebAction().get_search_result()
    SearchResults = res.get("result")
    Count = res.get("total")

    response = {
        "request": request,  
        "UserPris": str(pris).split(","),
        "Count": Count,
        "Results": SearchResults,
        "SiteDict": Indexer().get_indexer_hash_dict(),
        "UPCHAR": chr(8593)
        }

    return templates.TemplateResponse("search.html", response)


# 电影订阅页面
@App.api_route('/movie_rss', methods=['POST', 'GET'])
@login_required
def movie_rss(request: Request):
    RssItems = WebAction().get_movie_rss_list().get("result")
    RuleGroups = {str(group["id"]): group["name"] for group in Filter().get_rule_groups()}
    DownloadSettings = Downloader().get_download_setting()
    return render_template("rss/movie_rss.html",
                           Count=len(RssItems),
                           RuleGroups=RuleGroups,
                           DownloadSettings=DownloadSettings,
                           Items=RssItems,
                           Type='MOV',
                           TypeName='电影'
                           )

    response = {
        "request": request,  
        "Count": len(RssItems),
        "RuleGroups": RuleGroups,
        "DownloadSettings": DownloadSettings,
        "Items": RssItems
        }

    return templates.TemplateResponse("rss/movie_rss.html", response)


# 电视剧订阅页面
@App.api_route('/tv_rss', methods=['POST', 'GET'])
@login_required
def tv_rss(request: Request):
    RssItems = WebAction().get_tv_rss_list().get("result")
    RuleGroups = {str(group["id"]): group["name"] for group in Filter().get_rule_groups()}
    DownloadSettings = Downloader().get_download_setting()
    return render_template("rss/movie_rss.html",
                           Count=len(RssItems),
                           RuleGroups=RuleGroups,
                           DownloadSettings=DownloadSettings,
                           Items=RssItems,
                           Type='TV',
                           TypeName='电视剧'
                           )

    response = {
        "request": request,  
        "Count": len(RssItems),
        "RuleGroups": RuleGroups,
        "DownloadSettings": DownloadSettings,
        "Items": RssItems
        }

    return templates.TemplateResponse("rss/tv_rss.html", response)


# 订阅历史页面
@App.api_route('/rss_history', methods=['POST', 'GET'])
@login_required
def rss_history(request: Request):
    mtype = request.query_params.get("t")
    RssHistory = WebAction().get_rss_history({"type": mtype}).get("result")

    response = {
        "request": request,  
        "Count": len(RssHistory),
        "Items": RssHistory,
        "Type": mtype
        }

    return templates.TemplateResponse("rss/rss_history.html", response)


# 订阅日历页面
@App.api_route('/rss_calendar', methods=['POST', 'GET'])
@login_required
def rss_calendar(request: Request):
    Today = datetime.datetime.strftime(datetime.datetime.now(), '%Y-%m-%d')
    # 电影订阅
    RssMovieItems = WebAction().get_movie_rss_items().get("result")
    # 电视剧订阅
    RssTvItems = WebAction().get_tv_rss_items().get("result")

    response = {
        "request": request,  
        "Today": Today,
        "RssMovieItems": RssMovieItems,
        "RssTvItems": RssTvItems
        }

    return templates.TemplateResponse("rss/rss_calendar.html", response)

# 索引站点页面
@App.api_route('/indexer', methods=['POST', 'GET'])
@login_required
def indexer(request: Request):
    indexers = Indexer().get_indexers(check=False)
    indexer_sites = SystemConfig().get(SystemConfigKey.UserIndexerSites)

    public_indexers = []
    for site in indexers:
        if site.public:
            site_info = {
                "id": site.id,
                "name": site.name,
                "domain": site.domain,
                "render": site.render,
                "source_type": site.source_type,
                "search_type": site.search_type,
                "downloader": site.downloader,
                "public": site.public,
                "proxy": site.proxy,
                "checked": site.id in indexer_sites
            }
            public_indexers.append(site_info)
   
    DownloadSettings = {did: attr["name"] for did, attr in Downloader().get_download_setting().items()}
    SourceTypes = { "MOVIE":'电影', "TV":'剧集', "ANIME":'动漫' }
    SearchTypes = { "title":'关键字', "en_name":'英文名', "douban_id":'豆瓣id', "imdb":'imdb id' }

    response = {
        "request": request,  
        "Config": Config().get_config(),
        "IsPublic": 1,
        "Indexers": public_indexers,
        "DownloadSettings": DownloadSettings,
        "SourceTypes": SourceTypes,
        "SearchTypes": SearchTypes
        }

    return templates.TemplateResponse("site/indexer.html", response)


@App.api_route('/ptindexer', methods=['POST', 'GET'])
@login_required
def ptindexer(request: Request):
    indexers = Indexer().get_indexers(check=False)
    indexer_sites = SystemConfig().get(SystemConfigKey.UserIndexerSites)

    private_indexers = []
    for site in indexers:
        if site.public:
            continue
        site_info = {
            "id": site.id,
            "name": site.name,
            "domain": site.domain,
            "render": site.render,
            "source_type": site.source_type,
            "search_type": site.search_type,
            "downloader": site.downloader,
            "public": site.public,
            "proxy": site.proxy,
            "checked": site.id in indexer_sites
        }
        private_indexers.append(site_info)
   
    DownloadSettings = {did: attr["name"] for did, attr in Downloader().get_download_setting().items()}
    SourceTypes = { "MOVIE":'电影', "TV":'剧集', "ANIME":'动漫' }
    SearchTypes = { "title":'关键字', "en_name":'英文名', "douban_id":'豆瓣id', "imdb":'imdb id' }

    response = {
        "request": request,  
        "Config": Config().get_config(),
        "IsPublic": 0,
        "Indexers": private_indexers,
        "DownloadSettings": DownloadSettings,
        "SourceTypes": SourceTypes,
        "SearchTypes": SearchTypes
        }

    return templates.TemplateResponse("site/indexer.html", response)

# 站点维护页面
@App.api_route('/site', methods=['POST', 'GET'])
@login_required
def sites(request: Request):
    CfgSites = Sites().get_sites()
    RuleGroups = {str(group["id"]): group["name"] for group in Filter().get_rule_groups()}
    DownloadSettings = {did: attr["name"] for did, attr in Downloader().get_download_setting().items()}
    CookieCloudCfg = SystemConfig().get(SystemConfigKey.CookieCloud)
    CookieUserInfoCfg = SystemConfig().get(SystemConfigKey.CookieUserInfo)
    return render_template("site/site.html",
                           Sites=CfgSites,
                           RuleGroups=RuleGroups,
                           DownloadSettings=DownloadSettings,
                           ChromeOk=True,
                           CookieCloudCfg=CookieCloudCfg,
                           CookieUserInfoCfg=CookieUserInfoCfg)

    response = {
        "request": request,  
        "Sites": CfgSites,
        "RuleGroups": RuleGroups,
        "DownloadSettings": DownloadSettings,
        "ChromeOk": ChromeOk,
        "CookieCloudCfg": CookieCloudCfg,
        "CookieUserInfoCfg": CookieUserInfoCfg
        }
    
    return templates.TemplateResponse("site/site.html", response)


# 站点资源主页面
@App.api_route('/sitelist', methods=['POST', 'GET'])
@login_required
def sitelist(request: Request):
    IndexerSites = Indexer().get_indexers(check=False)

    response = {
        "request": request,  
        "Sites": IndexerSites,
        "Count": len(IndexerSites)
        }

    return templates.TemplateResponse("site/sitelist.html", response)


# 唤起App中转页面
@App.api_route('/open', methods=['POST', 'GET'])
def open_app(request: Request):
    return templates.TemplateResponse("openapp.html", { "request": request })

# 站点资源页面
@App.api_route('/resources', methods=['POST', 'GET'])
@login_required
def resources(request: Request):

    query_params = request.query_params

    site_id = query_params.get("site")
    site_name = query_params.get("title")
    page = query_params.get("page") or 0
    keyword = query_params.get("keyword")
    Results = WebAction().list_site_resources({
        "id": site_id,
        "page": page,
        "keyword": keyword
    }).get("data") or []

    response = {
        "request": request,  
        "Results": Results,
        "SiteId": site_id,
        "Title": site_name,
        "KeyWord": keyword,
        "TotalCount": len(Results),
        "PageRange": range(0, 10),
        "CurrentPage": int(page),
        "TotalPage": 10
        }

    return templates.TemplateResponse("site/resources.html", response)


# 推荐页面
@App.api_route('/recommend', methods=['POST', 'GET'])
@login_required
def recommend(request: Request):

    query_params = request.query_params

    Type = query_params.get("type") or ""
    SubType = query_params.get("subtype") or ""
    Title = query_params.get("title") or ""
    SubTitle = query_params.get("subtitle") or ""
    CurrentPage = query_params.get("page") or 1
    Week = query_params.get("week") or ""
    TmdbId = query_params.get("tmdbid") or ""
    PersonId = query_params.get("personid") or ""
    Keyword = query_params.get("keyword") or ""
    Source = query_params.get("source") or ""
    FilterKey = query_params.get("filter") or ""
    Params = json.loads(query_params.get("params")) if query_params.get("params") else {}

    response = {
        "request": request,
        "Type": Type,
        "SubType": SubType,
        "Title": Title,
        "CurrentPage": CurrentPage,
        "Week": Week,
        "TmdbId": TmdbId,
        "PersonId": PersonId,
        "SubTitle": SubTitle,
        "Keyword": Keyword,
        "Source": Source,
        "Filter": FilterKey,
        "FilterConf": ModuleConf.DISCOVER_FILTER_CONF.get(FilterKey) if FilterKey else {},
        "Params": Params
        }

    return templates.TemplateResponse("discovery/recommend.html", response)


# 推荐页面
@App.api_route('/ranking', methods=['POST', 'GET'])
@login_required
def ranking(request: Request):
    return templates.TemplateResponse("discovery/ranking.html",
        { "request": request, "DiscoveryType":"RANKING"})


# 豆瓣电影
@App.api_route('/douban_movie', methods=['POST', 'GET'])
@login_required
def douban_movie(request: Request):

    response = {
        "request": request,
        "Type": "DOUBANTAG",
        "SubType": "MOV",
        "Title": "豆瓣电影",
        "Filter": "douban_movie",
        "FilterConf": ModuleConf.DISCOVER_FILTER_CONF.get('douban_movie')
        }
    
    return templates.TemplateResponse("discovery/recommend.html", response)


# 豆瓣电视剧
@App.api_route('/douban_tv', methods=['POST', 'GET'])
@login_required
def douban_tv(request: Request):

    response = {
        "request": request,
        "Type": "DOUBANTAG",
        "SubType": "TV",
        "Title": "豆瓣剧集",
        "Filter": "douban_tv",
        "FilterConf": ModuleConf.DISCOVER_FILTER_CONF.get('douban_tv')
        }

    return templates.TemplateResponse("discovery/recommend.html", response)


@App.api_route('/tmdb_movie', methods=['POST', 'GET'])
@login_required
def tmdb_movie(request: Request):

    response = {
        "request": request,
        "Type": "DISCOVER",
        "SubType": "MOV",
        "Title": "TMDB电影",
        "Filter": "tmdb_movie",
        "FilterConf": ModuleConf.DISCOVER_FILTER_CONF.get('tmdb_movie')
        }

    return templates.TemplateResponse("discovery/recommend.html", response)


@App.api_route('/tmdb_tv', methods=['POST', 'GET'])
@login_required
def tmdb_tv(request: Request):

    response = {
        "request": request,
        "Type": "DISCOVER",
        "SubType": "TV",
        "Title": "TMDB剧集",
        "Filter": "tmdb_tv",
        "FilterConf": ModuleConf.DISCOVER_FILTER_CONF.get('tmdb_tv')
        }

    return templates.TemplateResponse("discovery/recommend.html", response)


# Bangumi每日放送
@App.api_route('/bangumi', methods=['POST', 'GET'])
@login_required
def discovery_bangumi(request: Request):
    return templates.TemplateResponse("discovery/ranking.html",
        { "request": request, "DiscoveryType":"BANGUMI"})


# 媒体详情页面
@App.api_route('/media_detail', methods=['POST', 'GET'])
@login_required
def media_detail(request: Request):

    query_params = request.query_params

    TmdbId = query_params.get("id")
    Type = query_params.get("type")

    response = {
        "request": request,
        "TmdbId": TmdbId,
        "Type": Type
        }

    return templates.TemplateResponse("discovery/mediainfo.html", response)


# 演职人员页面
@App.api_route('/discovery_person', methods=['POST', 'GET'])
@login_required
def discovery_person(request: Request):

    query_params = request.query_params

    TmdbId = query_params.get("tmdbid")
    Title = query_params.get("title")
    SubTitle = query_params.get("subtitle")
    Type = query_params.get("type")
    Keyword = query_params.get("keyword")

    response = {
        "request": request,
        "TmdbId": TmdbId,
        "Title": Title,
        "SubTitle": SubTitle,
        "Type": Type,
        "Keyword": Keyword
        }

    return templates.TemplateResponse("discovery/person.html", response)


# 正在下载页面
@App.api_route('/downloading', methods=['POST', 'GET'])
@login_required
def downloading(request: Request):
    DispTorrents = WebAction().get_downloading().get("result")

    response = {
        "request": request,
        "DownloadCount": len(DispTorrents),
        "Torrents": DispTorrents
        }

    return templates.TemplateResponse("download/downloading.html", response)


# 近期下载页面
@App.api_route('/downloaded', methods=['POST', 'GET'])
@login_required
def downloaded(request: Request):
    CurrentPage = request.query_params.get("page") or 1

    response = {
        "request": request,
        "Type": 'DOWNLOADED',
        "Title": '近期下载',
        "CurrentPage": CurrentPage
        }

    return templates.TemplateResponse("discovery/recommend.html", response)


@App.api_route('/torrent_remove', methods=['POST', 'GET'])
@login_required
def torrent_remove(request: Request):
    Downloaders = Downloader().get_downloader_conf_simple()
    TorrentRemoveTasks = TorrentRemover().get_torrent_remove_tasks()

    response = {
        "request": request,
        "Downloaders": Downloaders,
        "DownloaderConfig": ModuleConf.TORRENTREMOVER_DICT,
        "Count": len(TorrentRemoveTasks),
        "TorrentRemoveTasks": TorrentRemoveTasks
        }

    return templates.TemplateResponse("download/torrent_remove.html", response)


# 数据统计页面
@App.api_route('/statistics', methods=['POST', 'GET'])
@login_required
def statistics(request: Request):

    query_params = request.query_params

    # 刷新单个site
    refresh_site = query_params.getlist("refresh_site")
    # 强制刷新所有
    refresh_force = True if query_params.get("refresh_force") else False
    # 总上传下载
    TotalUpload = 0
    TotalDownload = 0
    TotalSeedingSize = 0
    TotalSeeding = 0
    # 站点标签及上传下载
    SiteNames = []
    SiteUploads = []
    SiteDownloads = []
    SiteRatios = []
    SiteErrs = {}
    # 站点上传下载
    SiteData = SiteUserInfo().get_site_data(specify_sites=refresh_site, force=refresh_force)
    if isinstance(SiteData, dict):
        for name, data in SiteData.items():
            if not data:
                continue
            up = data.get("upload", 0)
            dl = data.get("download", 0)
            ratio = data.get("ratio", 0)
            seeding = data.get("seeding", 0)
            seeding_size = data.get("seeding_size", 0)
            err_msg = data.get("err_msg", "")

            SiteErrs.update({name: err_msg})

            if not up and not dl and not ratio:
                continue
            if not str(up).isdigit() or not str(dl).isdigit():
                continue
            if name not in SiteNames:
                SiteNames.append(name)
                TotalUpload += int(up)
                TotalDownload += int(dl)
                TotalSeeding += int(seeding)
                TotalSeedingSize += int(seeding_size)
                SiteUploads.append(int(up))
                SiteDownloads.append(int(dl))
                SiteRatios.append(round(float(ratio), 1))

    # 站点用户数据
    SiteUserStatistics = WebAction().get_site_user_statistics({"encoding": "DICT"}).get("data")

    response = {
        "request": request,
        "TotalDownload": TotalDownload,
        "TotalUpload": TotalUpload,
        "TotalSeedingSize": TotalSeedingSize,
        "TotalSeeding": TotalSeeding,
        "SiteDownloads": SiteDownloads,
        "SiteUploads": SiteUploads,
        "SiteRatios": SiteRatios,
        "SiteNames": SiteNames,
        "SiteErr": SiteErrs,
        "SiteUserStatistics": SiteUserStatistics
        }

    return templates.TemplateResponse("site/statistics.html", response)


# 刷流任务页面
@App.api_route('/brushtask', methods=['POST', 'GET'])
@login_required
def brushtask(request: Request):
    # 站点列表
    CfgSites = Sites().get_sites(brush=True)
    # 下载器列表
    Downloaders = Downloader().get_downloader_conf_simple()
    # 任务列表
    Tasks = BrushTask().get_brushtask_info()

    response = {
        "request": request,
        "Count": len(Tasks),
        "Sites": CfgSites,
        "Tasks": Tasks,
        "Downloaders": Downloaders
        }

    return templates.TemplateResponse("site/brushtask.html", response)


# 服务页面
@App.api_route('/service', methods=['POST', 'GET'])
@login_required
def service(request: Request):
    # 所有规则组
    RuleGroups = Filter().get_rule_groups()
    # 所有同步目录
    SyncPaths = Sync().get_sync_path_conf()

    # 所有服务
    Services = current_user.get_services()
    pt = Config().get_config('pt')
    # RSS订阅
    if "rssdownload" in Services:
        pt_check_interval = pt.get('pt_check_interval')
        if str(pt_check_interval).isdigit():
            tim_rssdownload = str(round(int(pt_check_interval) / 60)) + " 分钟"
            rss_state = 'ON'
        else:
            tim_rssdownload = ""
            rss_state = 'OFF'
        Services['rssdownload'].update({
            'time': tim_rssdownload,
            'state': rss_state,
        })

    # RSS搜索
    if "subscribe_search_all" in Services:
        search_rss_interval = pt.get('search_rss_interval')
        if str(search_rss_interval).isdigit():
            if int(search_rss_interval) < 3:
                search_rss_interval = 3
            tim_rsssearch = str(int(search_rss_interval)) + " 小时"
            rss_search_state = 'ON'
        else:
            tim_rsssearch = ""
            rss_search_state = 'OFF'
        Services['subscribe_search_all'].update({
            'time': tim_rsssearch,
            'state': rss_search_state,
        })

    # 下载文件转移
    if "pttransfer" in Services:
        pt_monitor = Downloader().monitor_downloader_ids
        if pt_monitor:
            tim_pttransfer = str(round(PT_TRANSFER_INTERVAL / 60)) + " 分钟"
            sta_pttransfer = 'ON'
        else:
            tim_pttransfer = ""
            sta_pttransfer = 'OFF'
        Services['pttransfer'].update({
            'time': tim_pttransfer,
            'state': sta_pttransfer,
        })

    # 目录同步
    if "sync" in Services:
        if Sync().monitor_sync_path_ids:
            Services['sync'].update({
                'state': 'ON'
            })
        # else:
        #     Services.pop('sync')

    # 系统进程
    if "processes" in Services:
        if not SystemUtils.is_docker() or not SystemUtils.get_all_processes():
            Services.pop('processes')

    response = {
        "request": request,
        "Count": len(Services),
        "RuleGroups": RuleGroups,
        "SyncPaths": SyncPaths,
        "SchedulerTasks": Services
        }

    return templates.TemplateResponse("service.html", response)


# 历史记录页面
@App.api_route('/history', methods=['POST', 'GET'])
@login_required
def history(request: Request):

    query_params = request.query_params

    pagenum = query_params.get("pagenum")
    keyword = query_params.get("s") or ""
    current_page = query_params.get("page")
    Result = WebAction().get_transfer_history({"keyword": keyword, "page": current_page, "pagenum": pagenum})
    PageRange = WebUtils.get_page_range(current_page=Result.get("currentPage"), total_page=Result.get("totalPage"))

    response = {
        "request": request,
        "TotalCount": Result.get("total"),
        "Count": len(Result.get("result")),
        "Historys": Result.get("result"),
        "Search": keyword,
        "CurrentPage": Result.get("currentPage"),
        "TotalPage": Result.get("totalPage"),
        "PageRange": PageRange,
        "PageNum": Result.get("currentPage")
        }

    return templates.TemplateResponse("rename/history.html", response)


# TMDB缓存页面
@App.api_route('/tmdbcache', methods=['POST', 'GET'])
@login_required
def tmdbcache(request: Request):

    query_params = request.query_params

    page_num = query_params.get("pagenum")
    if not page_num:
        page_num = 30
    search_str = query_params.get("s")
    if not search_str:
        search_str = ""
    current_page = query_params.get("page")
    if not current_page:
        current_page = 1
    else:
        current_page = int(current_page)
    total_count, tmdb_caches = MetaHelper().dump_meta_data(search_str, current_page, page_num)
    total_page = floor(total_count / page_num) + 1
    page_range = WebUtils.get_page_range(current_page=current_page, total_page=total_page)

    response = {
        "request": request,
        "TotalCount": total_count,
        "Count": len(tmdb_caches),
        "TmdbCaches": tmdb_caches,
        "Search": search_str,
        "CurrentPage": current_page,
        "TotalPage": total_page,
        "PageRange": page_range,
        "PageNum": page_num
        }

    return templates.TemplateResponse("rename/tmdbcache.html", response)


# 手工识别页面
@App.api_route('/unidentification', methods=['POST', 'GET'])
@login_required
def unidentification(request: Request):

    query_params = request.query_params

    pagenum = query_params.get("pagenum")
    keyword = query_params.get("s") or ""
    current_page = query_params.get("page")
    Result = WebAction().get_unknown_list_by_page({"keyword": keyword, "page": current_page, "pagenum": pagenum})
    PageRange = WebUtils.get_page_range(current_page=Result.get("currentPage"), total_page=Result.get("totalPage"))

    response = {
        "request": request,
        "TotalCount": Result.get("total"),
        "Count": len(Result.get("items")),
        "Items": Result.get("items"),
        "Search": keyword,
        "CurrentPage": Result.get("currentPage"),
        "TotalPage": Result.get("totalPage"),
        "PageRange": PageRange,
        "PageNum": Result.get("currentPage")
        }

    return templates.TemplateResponse("rename/unidentification.html", response)


# 文件管理页面
@App.api_route('/mediafile', methods=['POST', 'GET'])
@login_required
def mediafile(request: Request):
    media_default_path = Config().get_config('media').get('media_default_path')
    if media_default_path:
        DirD = media_default_path
    else:
        download_dirs = Downloader().get_download_visit_dirs()
        if download_dirs:
            try:
                DirD = os.path.commonpath(download_dirs).replace("\\", "/")
            except Exception as err:
                print(str(err))
                DirD = "/"
        else:
            DirD = "/"
    DirR = request.query_params.get("dir")

    response = {
        "request": request,
        "Dir": DirR or DirD
        }

    return templates.TemplateResponse("rename/mediafile.html", response)


# 基础设置页面
@App.api_route('/basic', methods=['POST', 'GET'])
@login_required
def basic(request: Request):
    proxy = Config().get_config('app').get("proxies", {}).get("http")
    if proxy:
        proxy = proxy.replace("http://", "")
    RmtModeDict = WebAction().get_rmt_modes()
    CustomScriptCfg = SystemConfig().get(SystemConfigKey.CustomScript)
    ScraperConf = SystemConfig().get(SystemConfigKey.UserScraperConf) or {}

    response = {
        "request": request,
        "Config": Config().get_config(),
        "Proxy": proxy,
        "RmtModeDict": RmtModeDict,
        "CustomScriptCfg": CustomScriptCfg,
        "ScraperNfo": ScraperConf.get("scraper_nfo") or {},
        "ScraperPic": ScraperConf.get("scraper_pic") or {},
        "MediaServerConf": ModuleConf.MEDIASERVER_CONF,
        "TmdbDomains": TMDB_API_DOMAINS
        }

    return templates.TemplateResponse("setting/basic.html", response)


# 自定义识别词设置页面
@App.api_route('/customwords', methods=['POST', 'GET'])
@login_required
def customwords(request: Request):
    groups = WebAction().get_customwords().get("result")

    response = {
        "request": request,
        "Groups": groups,
        "GroupsCount": len(groups)
        }

    return templates.TemplateResponse("setting/customwords.html", response)


# 目录同步页面
@App.api_route('/directorysync', methods=['POST', 'GET'])
@login_required
def directorysync(request: Request):
    RmtModeDict = WebAction().get_rmt_modes()
    SyncPaths = Sync().get_sync_path_conf()

    response = {
        "request": request,
        "SyncPaths": SyncPaths,
        "SyncCount": len(SyncPaths),
        "RmtModeDict": RmtModeDict
        }

    return templates.TemplateResponse("setting/directorysync.html", response)

# 下载设置页面
@App.api_route('/download_setting', methods=['POST', 'GET'])
@login_required
def download_setting(request: Request):
    DefaultDownloadSetting = Downloader().default_download_setting_id
    Downloaders = Downloader().get_downloader_conf_simple()
    DownloadSetting = Downloader().get_download_setting()

    response = {
        "request": request,
        "DownloadSetting": DownloadSetting,
        "DefaultDownloadSetting": DefaultDownloadSetting,
        "Downloaders": Downloaders,
        "Count": len(DownloadSetting)
        }

    return templates.TemplateResponse("setting/download_setting.html", response)

# 媒体库页面
@App.route('/library', methods=['POST', 'GET'])
@login_required
def library(request: Request):

    response = {
        "request": request,
        "Config": Config().get_config(),
        "MediaServerConf": ModuleConf.MEDIASERVER_CONF
        }

    return templates.TemplateResponse("setting/library.html", response)

# 通知消息页面
@App.api_route('/notification', methods=['POST', 'GET'])
@login_required
def notification(request: Request):
    MessageClients = Message().get_message_client_info()
    Channels = ModuleConf.MESSAGE_CONF.get("client")
    Switchs = ModuleConf.MESSAGE_CONF.get("switch")

    response = {
        "request": request,
        "Channels": Channels,
        "Switchs": Switchs,
        "ClientCount": len(MessageClients),
        "MessageClients": MessageClients
        }

    return templates.TemplateResponse("setting/notification.html", response)


# 用户管理页面
@App.api_route('/users', methods=['POST', 'GET'])
@login_required
def users(request: Request):
    Users = WebAction().get_users().get("result")
    TopMenus = WebAction().get_top_menus().get("menus")

    response = {
        "request": request,
        "Users": Users,
        "UserCount": len(Users),
        "TopMenus": TopMenus
        }

    return templates.TemplateResponse("setting/users.html", response)


# 过滤规则设置页面
@App.api_route('/filterrule', methods=['POST', 'GET'])
@login_required
def filterrule(request: Request):
    result = WebAction().get_filterrules()

    response = {
        "request": request,
        "Count": len(result.get("ruleGroups")),
        "RuleGroups": result.get("ruleGroups"),
        "Init_RuleGroups": result.get("initRules")
        }

    return templates.TemplateResponse("setting/filterrule.html", response)


# 自定义订阅页面
@App.api_route('/user_rss', methods=['POST', 'GET'])
@login_required
def user_rss(request: Request):
    Tasks = RssChecker().get_rsstask_info()
    RssParsers = RssChecker().get_userrss_parser()
    RuleGroups = {str(group["id"]): group["name"] for group in Filter().get_rule_groups()}
    DownloadSettings = {did: attr["name"] for did, attr in Downloader().get_download_setting().items()}
    RestypeDict = ModuleConf.TORRENT_SEARCH_PARAMS.get("restype")
    PixDict = ModuleConf.TORRENT_SEARCH_PARAMS.get("pix")

    response = {
        "request": request,
        "Tasks": Tasks,
        "Count": len(Tasks),
        "RssParsers": RssParsers,
        "RuleGroups": RuleGroups,
        "RestypeDict": RestypeDict,
        "PixDict": PixDict,
        "DownloadSettings": DownloadSettings
        }

    return templates.TemplateResponse("rss/user_rss.html", response)


# RSS解析器页面
@App.api_route('/rss_parser', methods=['POST', 'GET'])
@login_required
def rss_parser(request: Request):
    RssParsers = RssChecker().get_userrss_parser()

    response = {
        "request": request,
        "RssParsers": RssParsers,
        "Count": len(RssParsers)
        }

    return templates.TemplateResponse("rss/rss_parser.html", response)


# 插件页面
@App.api_route('/plugin', methods=['POST', 'GET'])
@login_required
def plugin(request: Request):
    # 下载器
    DefaultDownloader = Downloader().default_downloader_id
    Downloaders = Downloader().get_downloader_conf()
    DownloadersCount = len(Downloaders)
    Categories = {
        x: WebAction().get_categories({
            "type": x
        }).get("category") for x in ["电影", "电视剧", "动漫"]
    }
    RmtModeDict = WebAction().get_rmt_modes()
    # 插件
    Plugins = WebAction().get_plugins_conf().get("result")
    Settings = '\n'.join(SystemConfig().get(SystemConfigKey.ExternalPluginsSource) or [])

    response = {
        "request": request,
        "Config": Config().get_config(),
        "Downloaders": Downloaders,
        "DefaultDownloader": DefaultDownloader,
        "DownloadersCount": DownloadersCount,
        "Categories": Categories,
        "RmtModeDict": RmtModeDict,
        "DownloaderConf": ModuleConf.DOWNLOADER_CONF,
        "Plugins": Plugins,
        "Settings": Settings,
        "PluginCount": len(Plugins)
        }

    return templates.TemplateResponse("setting/plugin.html", response)


# 事件响应
@App.api_route('/do', methods=['POST'])
@login_required
def do(request: Request, data: dict = Body(...)):
    try:
        # content = request.json()
        cmd = data.get("cmd")
        content = data.get("data") or {}
        return WebAction().action(cmd, content)
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        return {"code": -1, "msg": str(e)}


# 目录事件响应
@App.api_route('/dirlist', methods=['POST'])
@login_required
async def dirlist(request: Request):
    r = ['<ul class="jqueryFileTree" style="display: none;">']
    try:
        r = ['<ul class="jqueryFileTree" style="display: none;">']
        form_data = await request.form()
        in_dir = unquote(form_data.get('dir'))
        ft = form_data.get("filter")
        if not in_dir or in_dir == "/":
            if SystemUtils.get_system() == OsType.WINDOWS:
                partitions = SystemUtils.get_windows_drives()
                if partitions:
                    dirs = partitions
                else:
                    dirs = [os.path.join("C:/", f) for f in os.listdir("C:/")]
            else:
                dirs = [os.path.join("/", f) for f in os.listdir("/")]
        else:
            d = os.path.normpath(urllib.parse.unquote(in_dir))
            if not os.path.isdir(d):
                d = os.path.dirname(d)
            dirs = [os.path.join(d, f) for f in os.listdir(d)]
        for ff in dirs:
            f = os.path.basename(ff)
            if not f:
                f = ff
            if os.path.isdir(ff):
                r.append('<li class="directory collapsed"><a rel="%s/">%s</a></li>' % (
                    ff.replace("\\", "/"), f.replace("\\", "/")))
            else:
                if ft != "HIDE_FILES_FILTER":
                    e = os.path.splitext(f)[1][1:]
                    r.append('<li class="file ext_%s"><a rel="%s">%s</a></li>' % (
                        e, ff.replace("\\", "/"), f.replace("\\", "/")))
        r.append('</ul>')
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        r.append('加载路径失败: %s' % str(e))
    r.append('</ul>')
    return make_response(''.join(r), 200)


# 禁止搜索引擎
@App.api_route('/robots.txt', methods=['GET', 'POST'])
def robots():
    return FileResponse(path='web/robots.txt', media_type="text/plain")

# 响应企业微信消息
@App.get('/wechat')
def wechat(request: Request):
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.WX)
    if not interactive_client:
        return make_response("NAStool没有启用微信交互", 200)
    conf = interactive_client.get("config")
    sToken = conf.get('token')
    sEncodingAESKey = conf.get('encodingAESKey')
    sCorpID = conf.get('corpid')
    if not sToken or not sEncodingAESKey or not sCorpID:
        return
    wxcpt = WXBizMsgCrypt(sToken, sEncodingAESKey, sCorpID)
    query_params = request.query_params
    sVerifyMsgSig = query_params.get("msg_signature")
    sVerifyTimeStamp = query_params.get("timestamp")
    sVerifyNonce = query_params.get("nonce")

    if request.method == 'GET':
        if not sVerifyMsgSig and not sVerifyTimeStamp and not sVerifyNonce:
            return "NAStool微信交互服务正常！<br>微信回调配置步聚：<br>1、在微信企业应用接收消息设置页面生成Token和EncodingAESKey并填入设置->消息通知->微信对应项，打开微信交互开关。<br>2、保存并重启本工具，保存并重启本工具，保存并重启本工具。<br>3、在微信企业应用接收消息设置页面输入此地址：http(s)://IP:PORT/wechat（IP、PORT替换为本工具的外网访问地址及端口，需要有公网IP并做好端口转发，最好有域名）。"
        sVerifyEchoStr = query_params.get("echostr")
        log.info("收到微信验证请求: echostr= %s" % sVerifyEchoStr)
        ret, sEchoStr = wxcpt.VerifyURL(sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce, sVerifyEchoStr)
        if ret != 0:
            log.error("微信请求验证失败 VerifyURL ret: %s" % str(ret))
        # 验证URL成功，将sEchoStr返回给企业号
        return sEchoStr
    else:
        try:
            body = request.stream().read()  # 异步读取原始请求体数据
            sReqData = body.decode('utf-8')
            log.debug("收到微信请求：%s" % str(sReqData))
            ret, sMsg = wxcpt.DecryptMsg(sReqData, sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce)
            if ret != 0:
                log.error("解密微信消息失败 DecryptMsg ret = %s" % str(ret))
                return make_response("ok", 200)
            # 解析XML报文
            """
            1、消息格式：
            <xml>
               <ToUserName><![CDATA[toUser]]></ToUserName>
               <FromUserName><![CDATA[fromUser]]></FromUserName> 
               <CreateTime>1348831860</CreateTime>
               <MsgType><![CDATA[text]]></MsgType>
               <Content><![CDATA[this is a test]]></Content>
               <MsgId>1234567890123456</MsgId>
               <AgentID>1</AgentID>
            </xml>
            2、事件格式：
            <xml>
                <ToUserName><![CDATA[toUser]]></ToUserName>
                <FromUserName><![CDATA[UserID]]></FromUserName>
                <CreateTime>1348831860</CreateTime>
                <MsgType><![CDATA[event]]></MsgType>
                <Event><![CDATA[subscribe]]></Event>
                <AgentID>1</AgentID>
            </xml>            
            """
            dom_tree = xml.dom.minidom.parseString(sMsg.decode('UTF-8'))
            root_node = dom_tree.documentElement
            # 消息类型
            msg_type = DomUtils.tag_value(root_node, "MsgType")
            # Event event事件只有click才有效,enter_agent无效
            event = DomUtils.tag_value(root_node, "Event")
            # 用户ID
            user_id = DomUtils.tag_value(root_node, "FromUserName")
            # 没的消息类型和用户ID的消息不要
            if not msg_type or not user_id:
                log.info("收到微信心跳报文...")
                return make_response("ok", 200)
            # 解析消息内容
            content = ""
            if msg_type == "event" and event == "click":
                # 校验用户有权限执行交互命令
                if conf.get("adminUser") and not any(
                        user_id == admin_user for admin_user in str(conf.get("adminUser")).split(";")):
                    Message().send_channel_msg(channel=SearchType.WX, title="用户无权限执行菜单命令", user_id=user_id)
                    return make_response(content, 200)
                # 事件消息
                event_key = DomUtils.tag_value(root_node, "EventKey")
                if event_key:
                    log.info("点击菜单：%s" % event_key)
                    keys = event_key.split('#')
                    if len(keys) > 2:
                        content = ModuleConf.WECHAT_MENU.get(keys[2])
            elif msg_type == "text":
                # 文本消息
                content = DomUtils.tag_value(root_node, "Content", default="")
            if content:
                log.info(f"收到微信消息：userid={user_id}, text={content}")
                # 处理消息内容
                WebAction().handle_message_job(msg=content,
                            in_from=SearchType.WX,
                            user_id=user_id,
                            user_name=user_id)
            return make_response(content, 200)
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("微信消息处理发生错误：%s - %s" % (str(err), traceback.format_exc()))
            return make_response("ok", 200)

@App.post('/wechat')
async def wechat_post(request: Request):
    body = await request.body()  # 异步读取原始请求体数据

@App.api_route('/sendwechat', methods=['POST'])
@require_auth(force=False)
async def sendwechat(request: Request):
    if not SecurityHelper().check_mediaserver_ip(request.client.host):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.client.host}")
        return '不允许的IP地址请求'
    interactive_client = Message().get_interactive_client(SearchType.WX)
    if not interactive_client:
        return make_response("NAStool没有启用微信交互", 200)
    
    req_json = await request.json()
    title = req_json.get('title')
    if not title:
        return make_response("请设置消息标题", 200)

    message = req_json.get('message')
    if not message:
        return make_response("请填写消息内容", 200)
    
    Message().send_custom_message(clients=[str(interactive_client.get("id"))], title=title, text=message, image=req_json.get('image'))
    return make_response("ok", 200)


# Plex Webhook
@App.api_route('/plex', methods=['POST'])
@require_auth(force=False)
async def plex_webhook(request: Request):
    if not SecurityHelper().check_mediaserver_ip(request.client.host):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.client.host}")
        return '不允许的IP地址请求'
    
    form_data = await request.form()
    request_json = json.loads(form_data.get('payload', {}))
    log.debug("收到Plex Webhook报文：%s" % str(request_json))
    # 事件类型
    event_match = request_json.get("event") in ["media.play", "media.stop", "library.new"]
    # 媒体类型
    type_match = request_json.get("Metadata", {}).get("type") in ["movie", "episode", "show"]
    # 是否直播
    is_live = request_json.get("Metadata", {}).get("live") == "1"
    # 如果事件类型匹配,媒体类型匹配,不是直播
    if event_match and type_match and not is_live:
        # 发送消息
        ThreadHelper().start_thread(MediaServer().webhook_message_handler,
                 (request_json, MediaServerType.PLEX))
        # 触发事件
        EventManager().send_event(EventType.PlexWebhook, request_json)
    return 'Ok'


# Jellyfin Webhook
@App.api_route('/jellyfin', methods=['POST'])
@require_auth(force=False)
async def jellyfin_webhook(request: Request):
    if not SecurityHelper().check_mediaserver_ip(request.client.host):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.client.host}")
        return '不允许的IP地址请求'
    request_json = await request.json()
    log.debug("收到Jellyfin Webhook报文：%s" % str(request_json))
    # 发送消息
    ThreadHelper().start_thread(MediaServer().webhook_message_handler,
             (request_json, MediaServerType.JELLYFIN))
    # 触发事件
    EventManager().send_event(EventType.JellyfinWebhook, request_json)
    return 'Ok'


# Emby Webhook
@App.api_route('/emby', methods=['GET', 'POST'])
@require_auth(force=False)
async def emby_webhook(request: Request):
    if not SecurityHelper().check_mediaserver_ip(request.client.host):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.client.host}")
        return '不允许的IP地址请求'
    if request.method == 'POST':
        form_data = await request.form()
        log.debug("Emby Webhook data: %s" % str(form_data.get('data', {})))
        request_json = json.loads(form_data.get('data', {}))
    else:
        query_params = request.query_params
        request_json = dict(query_params)

    log.debug("收到Emby Webhook报文：%s" % str(request_json))
    # 发送消息
    ThreadHelper().start_thread(MediaServer().webhook_message_handler,
             (request_json, MediaServerType.EMBY))
    # 触发事件
    EventManager().send_event(EventType.EmbyWebhook, request_json)
    return 'Ok'


# Telegram消息响应
@App.api_route('/telegram', methods=['POST'])
@require_auth(force=False)
async def telegram(request: Request):
    """
    {
        'update_id': ,
        'message': {
            'message_id': ,
            'from': {
                'id': ,
                'is_bot': False,
                'first_name': '',
                'username': '',
                'language_code': 'zh-hans'
            },
            'chat': {
                'id': ,
                'first_name': '',
                'username': '',
                'type': 'private'
            },
            'date': ,
            'text': ''
        }
    }
    """
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.TG)
    if not interactive_client:
        return 'NAStool未启用Telegram交互'
    msg_json = await request.json()
    if not SecurityHelper().check_telegram_ip(request.client.host):
        log.error("收到来自 %s 的非法Telegram消息：%s" % (request.client.host, msg_json))
        return '不允许的IP地址请求'
    if msg_json:
        message = msg_json.get("message", {})
        text = message.get("text")
        user_id = message.get("from", {}).get("id")
        # 获取用户名
        user_name = message.get("from", {}).get("username")
        if text:
            log.info(f"收到Telegram消息：userid={user_id}, username={user_name}, text={text}")
            # 检查权限
            if text.startswith("/"):
                if str(user_id) not in interactive_client.get("client").get_admin():
                    Message().send_channel_msg(channel=SearchType.TG,
                            title="只有管理员才有权限执行此命令",
                            user_id=user_id)
                    return '只有管理员才有权限执行此命令'
            else:
                if str(user_id) not in interactive_client.get("client").get_users():
                    Message().send_channel_msg(channel=SearchType.TG,
                            title="你不在用户白名单中，无法使用此机器人",
                            user_id=user_id)
                    return '你不在用户白名单中，无法使用此机器人'
            # 处理消息
            WebAction().handle_message_job(msg=text,
                        in_from=SearchType.TG,
                        user_id=user_id,
                        user_name=user_name)
    return 'Ok'


# Synology Chat消息响应
@App.api_route('/synology', methods=['POST'])
@require_auth(force=False)
def synology(request: Request, data: dict = Body(...)):
    """
    token: bot token
    user_id
    username
    post_id
    timestamp
    text
    """
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.SYNOLOGY)
    if not interactive_client:
        return 'NAStool未启用Synology Chat交互'

    if not SecurityHelper().check_synology_ip(request.client.host):
        log.error("收到来自 %s 的非法Synology Chat消息：%s" % (request.client.host, data))
        return '不允许的IP地址请求'
    if data:
        token = data.get("token")
        if not interactive_client.get("client").check_token(token):
            log.error("收到来自 %s 的非法Synology Chat消息：token校验不通过！" % request.client.host)
            return 'token校验不通过'
        text = data.get("text")
        user_id = int(data.get("user_id"))
        # 获取用户名
        user_name = data.get("username")
        if text:
            log.info(f"收到Synology Chat消息：userid={user_id}, username={user_name}, text={text}")
            WebAction().handle_message_job(msg=text,
                        in_from=SearchType.SYNOLOGY,
                        user_id=user_id,
                        user_name=user_name)
    return 'Ok'


# Slack消息响应
@App.api_route('/slack', methods=['POST'])
@require_auth(force=False)
async def slack(request: Request):
    """
    # 消息
    {
        'client_msg_id': '',
        'type': 'message',
        'text': 'hello',
        'user': '',
        'ts': '1670143568.444289',
        'blocks': [{
            'type': 'rich_text',
            'block_id': 'i2j+',
            'elements': [{
                'type': 'rich_text_section',
                'elements': [{
                    'type': 'text',
                    'text': 'hello'
                }]
            }]
        }],
        'team': '',
        'client': '',
        'event_ts': '1670143568.444289',
        'channel_type': 'im'
    }
    # 快捷方式
    {
      "type": "shortcut",
      "token": "XXXXXXXXXXXXX",
      "action_ts": "1581106241.371594",
      "team": {
        "id": "TXXXXXXXX",
        "domain": "shortcuts-test"
      },
      "user": {
        "id": "UXXXXXXXXX",
        "username": "aman",
        "team_id": "TXXXXXXXX"
      },
      "callback_id": "shortcut_create_task",
      "trigger_id": "944799105734.773906753841.38b5894552bdd4a780554ee59d1f3638"
    }
    # 按钮点击
    {
      "type": "block_actions",
      "team": {
        "id": "T9TK3CUKW",
        "domain": "example"
      },
      "user": {
        "id": "UA8RXUSPL",
        "username": "jtorrance",
        "team_id": "T9TK3CUKW"
      },
      "api_app_id": "AABA1ABCD",
      "token": "9s8d9as89d8as9d8as989",
      "container": {
        "type": "message_attachment",
        "message_ts": "1548261231.000200",
        "attachment_id": 1,
        "channel_id": "CBR2V3XEX",
        "is_ephemeral": false,
        "is_app_unfurl": false
      },
      "trigger_id": "12321423423.333649436676.d8c1bb837935619ccad0f624c448ffb3",
      "client": {
        "id": "CBR2V3XEX",
        "name": "review-updates"
      },
      "message": {
        "bot_id": "BAH5CA16Z",
        "type": "message",
        "text": "This content can't be displayed.",
        "user": "UAJ2RU415",
        "ts": "1548261231.000200",
        ...
      },
      "response_url": "https://hooks.slack.com/actions/AABA1ABCD/1232321423432/D09sSasdasdAS9091209",
      "actions": [
        {
          "action_id": "WaXA",
          "block_id": "=qXel",
          "text": {
            "type": "plain_text",
            "text": "View",
            "emoji": true
          },
          "value": "click_me_123",
          "type": "button",
          "action_ts": "1548426417.840180"
        }
      ]
    }
    """
    # 只有本地转发请求能访问
    if not SecurityHelper().check_slack_ip(request.client.host):
        log.warn(f"非法IP地址的Slack消息通知：{request.client.host}")
        return '不允许的IP地址请求'

    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.SLACK)
    if not interactive_client:
        return 'NAStool未启用Slack交互'
    msg_json = await request.json()
    if msg_json:
        if msg_json.get("type") == "message":
            userid = msg_json.get("user")
            text = msg_json.get("text")
            username = msg_json.get("user")
        elif msg_json.get("type") == "block_actions":
            userid = msg_json.get("user", {}).get("id")
            text = msg_json.get("actions")[0].get("value")
            username = msg_json.get("user", {}).get("name")
        elif msg_json.get("type") == "event_callback":
            userid = msg_json.get('event', {}).get('user')
            text = re.sub(r"<@[0-9A-Z]+>", "", msg_json.get("event", {}).get("text"), flags=re.IGNORECASE).strip()
            username = ""
        elif msg_json.get("type") == "shortcut":
            userid = msg_json.get("user", {}).get("id")
            text = msg_json.get("callback_id")
            username = msg_json.get("user", {}).get("username")
        else:
            return "Error"
        log.info(f"收到Slack消息：userid={userid}, username={username}, text={text}")
        WebAction().handle_message_job(msg=text,
                    in_from=SearchType.SLACK,
                    user_id=userid,
                    user_name=username)
    return "Ok"


# Jellyseerr Overseerr订阅接口
@App.api_route('/subscribe', methods=['POST'])
@require_auth
async def subscribe(request: Request):
    """
    {
        "notification_type": "{{notification_type}}",
        "event": "{{event}}",
        "subject": "{{subject}}",
        "message": "{{message}}",
        "image": "{{image}}",
        "{{media}}": {
            "media_type": "{{media_type}}",
            "tmdbId": "{{media_tmdbid}}",
            "tvdbId": "{{media_tvdbid}}",
            "status": "{{media_status}}",
            "status4k": "{{media_status4k}}"
        },
        "{{request}}": {
            "request_id": "{{request_id}}",
            "requestedBy_email": "{{requestedBy_email}}",
            "requestedBy_username": "{{requestedBy_username}}",
            "requestedBy_avatar": "{{requestedBy_avatar}}"
        },
        "{{issue}}": {
            "issue_id": "{{issue_id}}",
            "issue_type": "{{issue_type}}",
            "issue_status": "{{issue_status}}",
            "reportedBy_email": "{{reportedBy_email}}",
            "reportedBy_username": "{{reportedBy_username}}",
            "reportedBy_avatar": "{{reportedBy_avatar}}"
        },
        "{{comment}}": {
            "comment_message": "{{comment_message}}",
            "commentedBy_email": "{{commentedBy_email}}",
            "commentedBy_username": "{{commentedBy_username}}",
            "commentedBy_avatar": "{{commentedBy_avatar}}"
        },
        "{{extra}}": []
    }
    """
    req_json = await request.json()
    if not req_json:
        return make_response("非法请求！", 400)
    notification_type = req_json.get("notification_type")
    if notification_type not in ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"]:
        return make_response("ok", 200)
    subject = req_json.get("subject")
    media_type = MediaType.MOVIE if req_json.get("media", {}).get("media_type") == "movie" else MediaType.TV
    tmdbId = req_json.get("media", {}).get("tmdbId")
    if not media_type or not tmdbId or not subject:
        return make_response("请求参数不正确！", 500)
    # 添加订阅
    code = 0
    msg = "ok"
    meta_info = MetaInfo(title=subject, mtype=media_type)
    user_name = req_json.get("request", {}).get("requestedBy_username")
    if media_type == MediaType.MOVIE:
        code, msg, _ = Subscribe().add_rss_subscribe(mtype=media_type,
                                  name=meta_info.get_name(),
                                  year=meta_info.year,
                                  channel=RssType.Auto,
                                  mediaid=tmdbId,
                                  in_from=SearchType.API,
                                  user_name=user_name)
    else:
        seasons = []
        for extra in req_json.get("extra", []):
            if extra.get("name") == "Requested Seasons":
                seasons = [int(str(sea).strip()) for sea in extra.get("value").split(", ") if str(sea).isdigit()]
                break
        for season in seasons:
            code, msg, _ = Subscribe().add_rss_subscribe(mtype=media_type,
                   name=meta_info.get_name(),
                   year=meta_info.year,
                   channel=RssType.Auto,
                   mediaid=tmdbId,
                   season=season,
                   in_from=SearchType.API,
                   user_name=user_name)
    if code == 0:
        return make_response("ok", 200)
    else:
        return make_response(msg, 500)

@App.api_route('/ical')
@require_auth(force=False)
def ical(request: Request):
    # 是否设置提醒开关
    remind = request.query_params.get("remind")
    cal = Calendar()
    RssItems = WebAction().get_ical_events().get("result")
    for item in RssItems:
        event = Event()
        event.add('summary', f'{item.get("type")}：{item.get("title")}')
        if not item.get("start"):
            continue
        event.add('dtstart',
                  datetime.datetime.strptime(item.get("start"), '%Y-%m-%d')
                  + datetime.timedelta(hours=8))
        event.add('dtend',
                  datetime.datetime.strptime(item.get("start"), '%Y-%m-%d')
                  + datetime.timedelta(hours=9))

        # 添加事件提醒
        if remind:
            alarm = Alarm()
            alarm.add('trigger', datetime.timedelta(minutes=30))
            alarm.add('action', 'DISPLAY')
            event.add_component(alarm)

        cal.add_component(event)

    # 返回日历文件
    response = Response(content=cal.to_ical(), media_type='text/calendar', status_code=200)
    response.headers['Content-Disposition'] = 'attachment; filename=nastool.ics'
    return response


# 备份配置文件
@App.api_route('/backup', methods=['POST'])
@login_required
def backup():
    """
    备份用户设置文件
    :return: 备份文件.zip_file
    """
    zip_file = WebAction().backup()
    if not zip_file:
        return make_response("创建备份失败", 400)
    return FileResponse(zip_file, media_type='application/octet-stream', filename = os.path.basename(zip_file))


# 上传文件到服务器
@App.api_route('/upload', methods=['POST'])
@login_required
def upload(file: UploadFile = File(...)):
    try:
        files = file.file.read()
        temp_path = Config().get_temp_path()
        if not os.path.exists(temp_path):
            os.makedirs(temp_path)
        file_path = Path(temp_path) / files.filename
        files.save(str(file_path))
        return {"code": 0, "filepath": str(file_path)}
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        return {"code": 1, "msg": str(e), "filepath": ""}


@App.api_route('/img')
@login_required
def Img(request: Request):
    """
    图片中换服务
    """
    url = request.query_params.get('url')
    if not url:
        return make_response("参数错误", 400)
    # 计算Etag
    etag = hashlib.sha256(url.encode('utf-8')).hexdigest()
    # 检查协商缓存
    if_none_match = request.headers.get('If-None-Match')
    if if_none_match and if_none_match == etag:
        return make_response('', 304)
    
    headers={
        'Cache-Control': 'max-age=604800',
        'Etag': etag
    }
    # 获取图片数据
    response = Response(
        headers=headers,
        content=WebUtils.request_cache(url),
        status_code=200, 
        media_type='image/jpeg'        
    )

    return response


@App.get('/stream-logging')
@login_required
def stream_logging(request: Request):
    """
    实时日志EventSources响应
    """
    def __logging(_source=""):
        """
        实时日志
        """
        global LoggingSource
        while True:
            logs = []
            with LoggingLock:
                if _source != LoggingSource:
                    LoggingSource = _source
                    log.LOG_INDEX = len(log.LOG_QUEUE)  # 更新索引
                if log.LOG_INDEX > 0:
                    # 获取最新的日志
                    logs = list(log.LOG_QUEUE)[-log.LOG_INDEX:]
                    log.LOG_INDEX = 0  # 重置索引
                    if _source:
                        # 根据source过滤日志
                        logs = [lg for lg in logs if lg.get("source") == _source]
            
            yield f'{json.dumps(logs)}\n\n'
            asyncio.sleep(1)

    type_param = request.query_params.get("type") or ""
    response = EventSourceResponse(__logging(type_param))

    # 设置响应头部信息
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Encoding"] = "identity"  
    response.headers["X-Accel-Buffering"] = "no"  # 禁用缓冲

    return response

@App.get('/stream-progress')
# @login_required
async def stream_progress(request: Request):
    """
    实时进度EventSources响应
    """
    async def __progress(_type):
        """
        实时进度
        """
        WA = WebAction()
        while True:
            await asyncio.sleep(0.2)
            detail = WA.refresh_process({"type": _type})
            yield f'{json.dumps(detail)}\n\n'
   
    type_param = request.query_params.get("type") or "" # 获取查询参数
    response = EventSourceResponse(__progress(type_param))

    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Encoding"] = "identity"  
    response.headers["X-Accel-Buffering"] = "no"  # 禁用缓冲
    
    return response


@App.websocket('/message')
async def message_handler(ws: WebSocket):
    """
    消息中心WebSocket
    """
    # 接受连接
    await ws.accept()

    # 用户校验
    if not current_user:
        ws.send_text("Authentication required.")
        ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    
    while True:
        try:
            data = ws.receive(timeout=10)
        except ConnectionClosed:
            print("WebSocket连接已关闭！")
            break
        if not data:
            continue
        try:
            msgbody = json.loads(data)
        except Exception as err:
            print(str(err))
            continue
        if msgbody.get("text"):
            # 发送的消息
            WebAction().handle_message_job(msg=msgbody.get("text"),
                                           in_from=SearchType.WEB,
                                           user_id=current_user.username,
                                           user_name=current_user.username)
            ws.send((json.dumps({})))
        else:
            # 拉取消息
            system_msg = WebAction().get_system_message(lst_time=msgbody.get("lst_time"))
            messages = system_msg.get("message")
            lst_time = system_msg.get("lst_time")
            ret_messages = []
            for message in list(reversed(messages)):
                content = re.sub(r"#+", "<br>",
                                 re.sub(r"<[^>]+>", "",
                                        re.sub(r"<br/?>", "####", message.get("content"), flags=re.IGNORECASE)))
                ret_messages.append({
                    "level": "bg-red" if message.get("level") == "ERROR" else "",
                    "title": message.get("title"),
                    "content": content,
                    "time": message.get("time")
                })
            ws.send((json.dumps({
                "lst_time": lst_time,
                "message": ret_messages
            })))


# base64模板过滤器
# @App.template_filter('b64encode')
def b64encode(s):
    return base64.b64encode(s.encode()).decode()


# split模板过滤器
# @App.template_filter('split')
def split(string, char, pos):
    return string.split(char)[pos]


# 刷流规则过滤器
# @App.template_filter('brush_rule_string')
def brush_rule_string(rules):
    return WebAction.parse_brush_rule_string(rules)


# 大小格式化过滤器
# @App.template_filter('str_filesize')
def str_filesize(size):
    return StringUtils.str_filesize(size, pre=1)


# MD5 HASH过滤器
# @App.template_filter('hash')
def md5_hash(text):
    return StringUtils.md5_hash(text)

# 注册自定义过滤器到 Jinja2 环境中
templates.env.filters['b64encode'] = b64encode
templates.env.filters['split'] = split
templates.env.filters['brush_rule_string'] = brush_rule_string
templates.env.filters['str_filesize'] = str_filesize
templates.env.filters['hash'] = md5_hash