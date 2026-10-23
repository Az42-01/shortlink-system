"""短链接创建表单。"""

from django import forms

from .utils import normalize_url


class ShortLinkForm(forms.Form):
    """只接收一个原始链接字段，输入时会自动补全 http:// 协议头。"""

    original_url = forms.CharField(
        label="原始链接",
        max_length=2048,
        strip=True,
        error_messages={"required": "请填写需要缩短的链接"},
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-lg",
                "placeholder": "https://example.com/very/long/path?utm_source=demo",
                "autocomplete": "off",
                "autofocus": True,
                "inputmode": "url",
                "id": "id_original_url",
            }
        ),
    )

    def clean_original_url(self) -> str:
        raw_url = self.cleaned_data["original_url"]
        try:
            return normalize_url(raw_url)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
