"""登录、令牌与角色权限。三个角色：经营分析师、业务负责人/管理层、系统与数据管理员。"""
import base64
import hashlib
import hmac
import os
import time

from . import db
from .config import SECRET

PERMISSIONS = {
    "dash:view": "查看指标异动大盘",
    "analysis:run": "创建归因会话、追问、审核证据链、调整 What-If",
    "analysis:view": "查看归因会话",
    "report:edit": "编辑报告、批注、发布",
    "report:view": "查看已发布报告",
    "report:export": "导出报告",
    "brief:subscribe": "订阅周期性归因简报",
    "events:view": "查看业务事件日历",
    "events:edit": "维护业务事件日历",
    "metrics:view": "查看指标字典",
    "metrics:edit": "维护指标元数据与拆解公式",
    "settings:edit": "配置模型、数据源与沙箱配额",
    "audit:view": "查看操作审计与运行日志",
}
ROLES = {
    "analyst": ("经营分析师", ["dash:view", "analysis:run", "analysis:view", "report:edit", "report:view", "report:export", "events:view", "metrics:view"]),
    "manager": ("业务负责人 / 管理层", ["dash:view", "analysis:view", "report:view", "report:export", "brief:subscribe", "events:view", "metrics:view"]),
    "admin": ("系统与数据管理员", ["dash:view", "analysis:view", "report:view", "events:view", "events:edit", "metrics:view", "metrics:edit", "settings:edit", "audit:view"]),
}
TOKEN_HOURS = 12


def hash_password(pw, salt=None):
    salt = salt or os.urandom(8).hex()
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 60000).hex()}"


def check_password(pw, stored):
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(pw, salt), stored)


def make_token(uid):
    exp = int(time.time()) + TOKEN_HOURS * 3600
    payload = f"{uid}.{exp}"
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def parse_token(token):
    try:
        uid, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".")
        good = hmac.new(SECRET.encode(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


def user_ctx(uid):
    u = db.q1("SELECT id, username, name, role, status FROM users WHERE id=?", (uid,))
    if not u or u["status"] != "active":
        return None
    name, perms = ROLES[u["role"]]
    return dict(u, role_name=name, perms=perms)


def seed_users():
    if db.q1("SELECT 1 FROM users"):
        return
    for un, nm, role, pw in (("analyst", "林晓（经营分析师）", "analyst", "analyst123"), ("manager", "周总（业务负责人）", "manager", "manager123"),
                             ("admin", "陈工（数据管理员）", "admin", "admin123")):
        db.x("INSERT INTO users (username, name, password_hash, role, created_at) VALUES (?,?,?,?,?)", (un, nm, hash_password(pw), role, db.now()))
