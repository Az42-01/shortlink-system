"""shortener 应用的路由。

注意顺序：静态路径（首页 / 统计 / 健康检查）必须放在短码通配之前，
否则 ``/healthz/`` 会被当成短码去查库。
"""

from django.urls import path, re_path

from . import views
from .utils import SHORT_CODE_PATTERN

app_name = "shortener"

urlpatterns = [
    # 创建短链（GET 展示表单，POST 提交）
    path("", views.create_link, name="create_link"),
    # 点击统计
    path("stats/<str:short_code>/", views.link_stats, name="link_stats"),
    # 点击统计数据接口（JSON，看板图表使用）
    path("stats/<str:short_code>/api/", views.link_stats_api, name="link_stats_api"),
    # 健康检查：数据库 + Redis 状态
    path("healthz/", views.health_check, name="healthz"),
    # 短码跳转，必须放在最后；要求带结尾斜杠，例如 /aB3xK/
    re_path(rf"^(?P<short_code>{SHORT_CODE_PATTERN})/$", views.redirect_link, name="redirect"),
]
