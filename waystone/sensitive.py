"""凭据特征检测：客户端离线预览与服务端发布共用同一条规则。只是辅助拦截，不能保证发现所有秘密。"""
import re

# 依次匹配：私钥文件头；sk- 开头的 API key；api_key/password/token/secret 后接 12 位以上取值的赋值写法（键名可带引号，兼容 JSON/YAML）。
SECRET_PATTERN=re.compile(r'(?i)(-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{12,}|(?:api[_-]?key|password|token|secret)["\x27]?\s*[:=]\s*["\x27]?[A-Za-z0-9_\-/+]{12,})')

def looks_like_secret(text):return bool(SECRET_PATTERN.search(text))
