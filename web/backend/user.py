from werkzeug.security import check_password_hash
from pydantic import BaseModel

from config import Config


class User(BaseModel):
    """
    用户
    """

    id: str = ''
    username: str = ''
    password_hash: str = ''
    pris: str = ''
    search: int = 10
    level: int = 0
    admin: int = 0

    def __init__(self, user=None):
        if user:           
            super().__init__()
            self.id = user.get('id')
            self.username = user.get('name')
            self.password_hash = user.get('password')
            self.pris = user.get('pris')
            self.search = 1
            self.level = 99
            self.admin = 1 if '系统设置' in self.pris else 0


    # 验证密码
    def verify_password(self, password):
        if self.password_hash is None:
            return False
        return check_password_hash(self.password_hash, password)

    # 查询顶底菜单列表
    def get_topmenus(self):
        return self.pris.split(',')

    # 查询用户可用菜单
    def get_usermenus(self):
        if self.admin:
            return Config().menu
        menu = self.get_topmenus()
        return list(filter(lambda x: x.get("name") in menu, Config().menu))

    # 查询服务
    def get_services(self):
        return Config().services

    # 获取所有认证站点
    def get_authsites(self):
        return []

    # 新增用户
    def add_user(self, name, password, pris):
        try:
            self.dbhelper.insert_user(name, password, pris)
            return 1
        except Exception as e:
            print("新增用户出现严重错误！请检查：%s" % str(e))
            return 0

    # 删除用户
    def delete_user(self, name):
        try:
            self.dbhelper.delete_user(name)
            return 1
        except Exception as e:
            print("删除用户出现严重错误！请检查：%s" % str(e))
            return 0

    # 检查用户是否验证通过
    def check_user(self, site, param):
        return 1, ''


