"""短链接系统的通用工具函数。

包含三个部分：
    1. Base62 编解码：把数据库自增 ID 转成 4~6 位短码（双射，无碰撞）
    2. URL 规范化：允许用户输入 "example.com/a" 这类省略协议头的地址
    3. 访问信息解析：从请求头里安全地提取客户端 IP / User-Agent

Base62 设计说明
---------------
自增 ID 与短码之间是一一映射（双射），所以只要 ID 不同，短码一定不同，
不需要额外的唯一性重试逻辑：

    id = 1        -> "0001"
    id = 62       -> "0010"
    id = 238328   -> "1000"      # 62 ** 3
    id = 62**6-1  -> "ZZZZZZ"

因为 padding 只用最低位字符 '0' 补齐，长度不低于 MIN_CODE_LENGTH，
所以短码长度稳定在 4~6 位；容量上限 62**6 - 1 = 56,800,235,583 条链接。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Base62 常量
# ---------------------------------------------------------------------------
# 顺序固定，禁止随意改动：一旦改动，历史短码将无法解码回原 ID
BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(BASE62_ALPHABET)  # 62

MIN_CODE_LENGTH = 4
MAX_CODE_LENGTH = 6

# 自增 ID 能编码的最大值：62^6 - 1 = 56,800,235,583
MAX_ENCODABLE_ID = BASE**MAX_CODE_LENGTH - 1

# 允许出现在 URL 里的短码：4~6 位 Base62 字符
SHORT_CODE_PATTERN = rf"[0-9a-zA-Z]{{{MIN_CODE_LENGTH},{MAX_CODE_LENGTH}}}"
SHORT_CODE_RE = re.compile(rf"^{SHORT_CODE_PATTERN}$")

_USER_AGENT_MAX_LENGTH = 512


class ShortCodeError(ValueError):
    """短码编解码失败。"""


class ShortCodeOverflow(ShortCodeError):
    """自增 ID 超出 6 位 Base62 的表示范围。"""


# ---------------------------------------------------------------------------
# Base62 编解码
# ---------------------------------------------------------------------------
def encode_base62(number: int, min_length: int = MIN_CODE_LENGTH) -> str:
    """把非负整数编码为 Base62 字符串，并左补 '0' 到 ``min_length`` 位。

    >>> encode_base62(1)
    '0001'
    >>> encode_base62(62)
    '0010'
    """
    if not isinstance(number, int):
        raise ShortCodeError(f"number 必须是 int，收到 {type(number).__name__}")
    if number < 0:
        raise ShortCodeError(f"number 不能为负数：{number}")
    if number > MAX_ENCODABLE_ID:
        raise ShortCodeOverflow(
            f"ID {number} 超出 {MAX_CODE_LENGTH} 位 Base62 容量上限 {MAX_ENCODABLE_ID}"
        )
    if min_length < 1:
        raise ShortCodeError("min_length 必须 >= 1")

    if number == 0:
        encoded = BASE62_ALPHABET[0]
    else:
        digits = []
        while number > 0:
            number, remainder = divmod(number, BASE)
            digits.append(BASE62_ALPHABET[remainder])
        encoded = "".join(reversed(digits))

    if len(encoded) < min_length:
        encoded = BASE62_ALPHABET[0] * (min_length - len(encoded)) + encoded
    return encoded


def decode_base62(code: str) -> int:
    """把 Base62 字符串解码回整数，非法字符 / 空串会抛 ``ShortCodeError``。"""
    if not isinstance(code, str) or not code:
        raise ShortCodeError("短码不能为空")
    number = 0
    for char in code:
        try:
            value = BASE62_ALPHABET.index(char)
        except ValueError as exc:
            raise ShortCodeError(f"短码包含非法字符：{char!r}") from exc
        number = number * BASE + value
    return number


def generate_short_code(link_id: int) -> str:
    """把 ShortLink 的自增主键转换成对外暴露的短码。"""
    return encode_base62(link_id, min_length=MIN_CODE_LENGTH)


def is_valid_short_code(code: str) -> bool:
    """判断字符串是否是合法的短码（4~6 位 Base62）。"""
    return bool(code) and SHORT_CODE_RE.match(code) is not None


# ---------------------------------------------------------------------------
# URL 规范化
# ---------------------------------------------------------------------------
_ALLOWED_SCHEMES = {"http", "https"}
# 形如 "scheme:" 的前缀，用于识别 "javascript:..." / "ftp://..." 这类输入
_SCHEME_LIKE_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*):")


def normalize_url(raw_url: str) -> str:
    """补全协议头并校验，返回可直接入库的绝对 URL。

    - ``example.com/a``      -> ``http://example.com/a``
    - ``HTTPS://Example.COM`` -> ``https://example.com/``
    - ``ftp://x`` / ``javascript:alert(1)`` -> 抛 ValueError

    :raises ValueError: 协议不被支持或缺少域名
    """
    value = (raw_url or "").strip()
    if not value:
        raise ValueError("链接不能为空")

    if "://" in value:
        scheme = urlsplit(value).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(f"仅支持 http / https 链接，收到 {scheme or '空协议'}")
    else:
        # 没有 "://" 时先看是否写了协议前缀，例如 "http:example.com" / "javascript:alert(1)"
        match = _SCHEME_LIKE_RE.match(value)
        if match:
            prefix = match.group("scheme").lower()
            rest = value[match.end():]
            if prefix in _ALLOWED_SCHEMES:
                value = f"{prefix}://{rest.lstrip('/')}"
            elif not rest[:1].isdigit():
                # 数字开头视为 host:port（如 localhost:8000），其余一律拒绝
                raise ValueError(f"仅支持 http / https 链接，收到 {prefix}")

    candidate = value if "://" in value else f"http://{value}"
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("仅支持 http / https 链接")
    if not parts.netloc or parts.hostname is None:
        raise ValueError("链接缺少域名")
    if any(char.isspace() for char in parts.netloc):
        raise ValueError("链接域名中包含空格")

    # 统一小写 scheme/host，避免同一站点产生多条记录
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# 请求信息解析
# ---------------------------------------------------------------------------
def get_client_ip(request) -> str:
    """获取真实客户端 IP，取不到或格式非法时返回 "". """
    candidates = []
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        candidates.extend(part.strip() for part in forwarded_for.split(",") if part.strip())
    real_ip = request.META.get("HTTP_X_REAL_IP", "")
    if real_ip:
        candidates.append(real_ip.strip())
    remote_addr = request.META.get("REMOTE_ADDR", "")
    if remote_addr:
        candidates.append(remote_addr.strip())

    for candidate in candidates:
        # 去掉 IPv4 的 ":port" 形式（IPv6 用 [] 包裹，这里不处理端口）
        if candidate.count(":") == 1 and "." in candidate:
            candidate = candidate.rsplit(":", 1)[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return ""


def truncate_user_agent(user_agent: str) -> str:
    """截断超长 User-Agent，避免异常请求把日志表撑爆。"""
    value = (user_agent or "").strip()
    if len(value) <= _USER_AGENT_MAX_LENGTH:
        return value
    return value[:_USER_AGENT_MAX_LENGTH]


# ---------------------------------------------------------------------------
# User-Agent 设备解析（看板饼图用）
# ---------------------------------------------------------------------------
DEVICE_PC = "pc"
DEVICE_MOBILE = "mobile"
DEVICE_OTHER = "other"

# 顺序固定：API 返回、饼图图例都按这个顺序排列，前端可以按下标直接映射
DEVICE_TYPES = (DEVICE_PC, DEVICE_MOBILE, DEVICE_OTHER)

DEVICE_TYPE_LABELS = {
    DEVICE_PC: "PC",
    DEVICE_MOBILE: "移动端",
    DEVICE_OTHER: "其他",
}

# 移动端特征串；平板（iPad / Android Tablet）也归入移动端，看板只需要三分类
_MOBILE_UA_KEYWORDS = (
    "mobile",
    "iphone",
    "ipod",
    "ipad",
    "android",
    "tablet",
    "kindle",
    "silk",
    "windows phone",
    "iemobile",
    "blackberry",
    "opera mini",
    "harmonyos",
)

# 桌面操作系统特征串
_PC_UA_KEYWORDS = (
    "windows nt",
    "windows 95",
    "windows 98",
    "windows 9",
    "macintosh",
    "mac os x",
    "x11",
    "linux",
    "cros",
    "freebsd",
    "openbsd",
    "sunos",
)


def parse_device_type(user_agent: str) -> str:
    """把 User-Agent 粗分为 ``pc`` / ``mobile`` / ``other``。

    >>> parse_device_type("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)")
    'mobile'
    >>> parse_device_type("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    'pc'
    >>> parse_device_type("curl/8.5.0")
    'other'

    先判断移动端：iPhone 的 UA 里同样会出现 "Mac OS X"，
    先匹配桌面关键字会被误判成 PC。爬虫 / 命令行 / 空 UA 一律归为 ``other``。
    """
    ua = (user_agent or "").strip().lower()
    if not ua:
        return DEVICE_OTHER
    if any(keyword in ua for keyword in _MOBILE_UA_KEYWORDS):
        return DEVICE_MOBILE
    if any(keyword in ua for keyword in _PC_UA_KEYWORDS):
        return DEVICE_PC
    return DEVICE_OTHER


def device_label(device_type: str) -> str:
    """设备类型 -> 中文展示名，未知类型降级为「其他」。"""
    return DEVICE_TYPE_LABELS.get(device_type, DEVICE_TYPE_LABELS[DEVICE_OTHER])
