import contextvars
import inspect
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Optional

from fastapi import Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from werkzeug.local import LocalProxy

from app.helper.db_helper import DbHelper
from app.utils.commons import singleton
from config import Config
from web.backend.user import User

# 秘钥和算法配置
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# 密码加密上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 全局上下文变量
current_user_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("current_user_context", default=None)

# 当前用户方法代理
current_user = LocalProxy(lambda: get_current_user())

def try_login(username: str, password: str):
    user_info = UserManager().get_user(username)
    if not user_info:
        return ''
    
    # 将 current_user 存储到上下文变量
    current_user_context.set(user_info)
    if user_info.verify_password(password):
        # 创建用户 access_token
        access_token = create_access_token({"sub": username})
        return access_token
    return ''

# 创建访问令牌
def create_access_token(data: dict):
    expires_delta= timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = data.copy()
    expire = datetime.now() + expires_delta
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, Config().secret_key, algorithm=ALGORITHM)
    return encoded_jwt

# 验证用户
def authenticate_user(username: str, password: str):
    user = UserManager().get_user(username)
    if not user or not user.verify_password(password):
        return False
    return user

# 解码 JWT 并验证用户
def get_user_from_token(token: str) -> User:
    try:
        payload = jwt.decode(token, Config().secret_key, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            return None
        return UserManager().get_user(username)
    except JWTError:
        return None

# 获取当前用户的工具函数
def get_current_user() -> Optional[dict]:
    return current_user_context.get()

# 定义装饰器
def login_required(func: Callable[..., Any]):

    @wraps(func)
    async def async_wrapper(request: Request, *args, **kwargs):
         # 从Request对象获取Cookie
        access_token = request.cookies.get("access_token")
        if not access_token:
            return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        
        scheme, _, param = access_token.partition(" ")
        if scheme.lower() != "bearer":
            return None
        current_user = get_user_from_token(param)
        if current_user is None:
            return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

        current_user_context.set(current_user)
        kwargs['request'] = request
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(request: Request, *args, **kwargs):
         # 从Request对象获取Cookie
        access_token = request.cookies.get("access_token")
        if not access_token:
            return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        
        scheme, _, param = access_token.partition(" ")
        if scheme.lower() != "bearer":
            return None
        current_user = get_user_from_token(param)
        if current_user is None:
            return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

        current_user_context.set(current_user)
        kwargs['request'] = request
        return func(*args, **kwargs)
    
    
    # 选择合适的包装器
    if inspect.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper


@singleton
class UserManager():

    dbhelper = None
    admin_users = []

    def __init__(self):
        self.dbhelper = DbHelper()
        self.admin_users = [{
            "id": 0,
            "name": Config().get_config('app').get('login_user'),
            "password": Config().get_config('app').get('login_password')[6:],
            "pris": "我的媒体库,资源搜索,探索,站点管理,订阅管理,下载管理,媒体整理,服务,系统设置"
        }]

    # 根据用户名获取用户对像
    def get_user(self, user_name):
        for user in self.admin_users:
            if user.get("name") == user_name:
                return User(user)
            
        for user in self.dbhelper.get_users():
            if user.NAME == user_name:
                return User({"id": user.ID, "name": user.NAME, "password": user.PASSWORD, "pris": user.PRIS})
        return None

    # 查询用户列表
    def get_users(self):
        all_user = []
        for user in self.dbhelper.get_users():
            one = User({"id": user.ID, "name": user.NAME, "password": user.PASSWORD, "pris": user.PRIS})
            all_user.append(one)
        return all_user