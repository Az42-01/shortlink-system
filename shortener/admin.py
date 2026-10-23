"""Django admin：方便手工排查短链与点击日志。"""

from django.contrib import admin

from .models import ClickLog, ShortLink


@admin.register(ShortLink)
class ShortLinkAdmin(admin.ModelAdmin):
    list_display = ("short_code", "original_url", "click_count", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("short_code", "original_url")
    readonly_fields = ("click_count", "created_at")
    date_hierarchy = "created_at"


@admin.register(ClickLog)
class ClickLogAdmin(admin.ModelAdmin):
    list_display = ("link", "ip_address", "user_agent", "created_at")
    list_filter = ("created_at",)
    search_fields = ("link__short_code", "ip_address")
    raw_id_fields = ("link",)
    readonly_fields = ("link", "ip_address", "user_agent", "created_at")

    def has_add_permission(self, request):  # 点击日志只允许系统写入
        return False
