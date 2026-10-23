"""短链接系统的数据模型。

阶段一只有两张表：
    ShortLink  —— 长链接与短码的映射，附带累计点击次数（读多写多，热点字段）
    ClickLog   —— 每一次访问的明细，用于后续的分析报表
"""

from django.db import models, transaction
from django.urls import reverse

from . import utils


class ShortLinkQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class ShortLink(models.Model):
    """长链接与短码的映射关系。"""

    original_url = models.URLField("原始链接", max_length=2048)
    # short_code 允许为 NULL 的原因：
    # 短码由自增 ID 推导，必须先 INSERT 拿到主键，再回填短码。
    # SQLite / MySQL / PostgreSQL 的唯一索引都允许多个 NULL，因此不会互相冲突。
    short_code = models.CharField(
        "短码",
        max_length=utils.MAX_CODE_LENGTH,
        unique=True,
        null=True,
        blank=True,
        help_text="由自增 ID 经 Base62 编码生成，长度 4~6 位",
    )
    click_count = models.PositiveBigIntegerField("点击次数", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    objects = ShortLinkQuerySet.as_manager()

    class Meta:
        db_table = "short_link"
        verbose_name = "短链接"
        verbose_name_plural = "短链接"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.short_code or '(待生成)'} -> {self.original_url}"

    def get_absolute_url(self):
        return reverse("shortener:redirect", args=[self.short_code])

    # ------------------------------------------------------------------
    # 创建逻辑
    # ------------------------------------------------------------------
    @classmethod
    def create_with_short_code(cls, original_url: str) -> "ShortLink":
        """写入长链接并基于自增 ID 生成短码。

        整个过程放在一个事务里：拿到主键 -> 计算 Base62 短码 -> 回填。
        因为 Base62 对自增 ID 是双射，所以短码天然唯一，无需查重重试；
        这里仍然保留一次冲突探测，防止历史数据或手工导入造成主键冲突。

        :raises utils.ShortCodeOverflow: 自增 ID 超出 6 位 Base62 容量
        """
        with transaction.atomic():
            link = cls.objects.create(original_url=original_url)
            link.short_code = cls._unique_short_code(link.pk)
            link.save(update_fields=["short_code"])
        return link

    @classmethod
    def _unique_short_code(cls, link_id: int) -> str:
        code = utils.generate_short_code(link_id)
        if not cls.objects.filter(short_code=code).exists():
            return code

        # 理论上不可达（双射），兜底：偏移探测一个可用短码
        for offset in range(1, 1000):
            candidate = utils.generate_short_code(link_id + offset)
            if not cls.objects.filter(short_code=candidate).exists():
                return candidate
        raise utils.ShortCodeError(f"无法为 ID {link_id} 分配唯一短码")

    # ------------------------------------------------------------------
    # 展示辅助
    # ------------------------------------------------------------------
    @property
    def short_path(self) -> str:
        """短链的相对路径，例如 /aB3xK/，模板中再拼域名。"""
        return self.get_absolute_url()


class ClickLog(models.Model):
    """一次短链访问记录（由后台线程批量写入）。"""

    link = models.ForeignKey(
        ShortLink,
        on_delete=models.CASCADE,
        related_name="clicks",
        verbose_name="短链接",
    )
    ip_address = models.GenericIPAddressField("访问者 IP", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=512, blank=True, default="")
    created_at = models.DateTimeField("访问时间", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "click_log"
        verbose_name = "点击日志"
        verbose_name_plural = "点击日志"
        ordering = ["-created_at"]
        indexes = [
            # 统计页最常见的查询：某个短链最近的点击
            models.Index(fields=["link", "-created_at"], name="idx_click_link_time"),
        ]

    def __str__(self):
        return f"{self.link_id} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
