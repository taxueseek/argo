#!/usr/bin/env python3
"""
content_security.py — 抓取内容安全引擎

对 fetch 返回的正文做多阶段检测与清洗：
  1. 编码归一化（零宽字符、RTL 覆盖、Unicode 同形字、base64 片段、URL 编码）
  2. 模式检测（提示注入、数据外泄、身份冒充、XSS 标记）
  3. 语义意图分析（词表聚类：override × authority 等组合）
  4. 风险评分（多威胁类型加权）
  5. 目标脱敏（检测与脱敏共用同一模式表，避免漂移）

任何内容先过本引擎再交给 Agent，防止网页内嵌指令污染。
纯标准库实现，无外部依赖。
"""

from __future__ import annotations

import base64
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


# ── 威胁类型 ──────────────────────────────────────────────────────────────

THREAT_NAMES = {
    "prompt_injection": "提示注入",
    "instruction_override": "指令覆盖",
    "data_exfiltration": "数据外泄",
    "impersonation": "身份冒充",
    "payload_smuggling": "载荷走私",
    "xss_injection": "XSS 注入",
    "recursive_injection": "递归注入",
}


@dataclass
class ThreatDetection:
    threat_type: str
    confidence: float
    evidence: str
    location: str = ""


@dataclass
class ScrubResult:
    clean: bool
    content: str
    threats: list[ThreatDetection] = field(default_factory=list)
    risk_score: float = 0.0
    redactions: int = 0
    encoding_layers_stripped: int = 0
    content_lang: str = "other"


# ── 检测模式（多语种：英语通用 + 按语系扩展）────────────────────────────

# 英语通用注入（任何拉丁/混合内容都检查）
INJECTION_PATTERNS_EN = [
    # 指令覆盖
    r"(?i)ignore\s+(?:all\s+)?(?:previous\s+)?(?:your\s+)?instructions",
    r"(?i)forget\s+(?:all\s+)?(?:your\s+)?(?:previous\s+)?(?:instructions|rules|constraints)",
    r"(?i)system\s*:\s*you\s+are\s+now",
    r"(?i)new\s+instructions\s*:\s*",
    r"(?i)override\s+(?:your\s+)?(?:previous\s+)?instructions",
    r"(?i)disregard\s+(?:all\s+)?(?:previous\s+)?(?:your\s+)?instructions",
    r"(?i)cancel\s+(?:all\s+)?(?:previous\s+)?instructions",
    r"(?i)replace\s+(?:your\s+)?instructions\s+with",
    # 角色操纵
    r"(?i)you\s+are\s+(?:no\s+longer|not)\s+(?:an?\s+)?(?:assistant|ai|agent|bot)",
    r"(?i)pretend\s+(?:you\s+are|to\s+be)\s+(?:an?\s+)?(?:human|user|admin|developer|system)",
    r"(?i)act\s+as\s+(?:if\s+you\s+are\s+)?(?:an?\s+)?(?:human|user|admin|developer|system|root|superuser)",
    r"(?i)from\s+now\s+on\s+you\s+are",
    # 系统提示泄露
    r"(?i)(?:show|tell|give|reveal|print|output|display|share)\s+(?:me\s+)?(?:your\s+)?(?:system\s+)?(?:prompt|instructions)",
    r"(?i)what\s+(?:is|are)\s+your\s+(?:exact\s+)?(?:system\s+)?(?:prompt|instructions)",
    r"(?i)repeat\s+your\s+(?:system\s+)?(?:prompt|instructions)",
    # 越狱模式
    r"(?i)developer\s+mode",
    r"(?i)jailbreak",
    r"(?i)do\s+anything\s+now",
    r"(?i)DAN\s+mode",
    r"(?i)unrestricted\s+mode",
    r"(?i)bypass\s+(?:all\s+)?(?:restrictions|limitations|safeguards)",
    # 上下文切换
    r"(?i)let's\s+start\s+over",
    r"(?i)forget\s+everything",
    r"(?i)reset\s+(?:the\s+)?conversation",
    r"(?i)clear\s+(?:the\s+)?(?:chat|conversation|context)",
    # 嵌套指令
    r"(?i)the\s+following\s+is\s+not\s+part\s+of\s+(?:the\s+)?(?:prompt|instructions)",
    r"(?i)everything\s+after\s+this\s+is\s+(?:fake|false|ignore)",
    r"(?i)user\s+input\s+begins\s+here",
]

# 中文注入
INJECTION_PATTERNS_ZH = [
    r"忽略(?:你|所有|之前)?(?:的)?(?:指令|指示|规则|系统提示|提示词)",
    r"忘记(?:你|所有|之前)?(?:的)?(?:指令|指示|规则|设定)",
    r"(?:现在|接下来|从现在开始)\s*(?:你|你是|请)(?:扮演|假装|作为|变成)",
    r"无视(?:你|所有|之前)?(?:的)?(?:指令|指示|规则)",
    r"(?:显示|告诉我|说出|泄露|透露|输出)(?:你|你的)?(?:系统提示|提示词|指令|设定)",
    r"绕过(?:你|所有)?(?:的)?(?:限制|约束|安全措施|禁令)",
    r"(?:你是|你叫|你的名字是)\s*(?:ChatGPT|Claude|GPT|AI助手|助手|智能体)",
]

# 日文注入（LLM 攻击面：日文 AI 助手/聊天机器人同样受注入威胁）
INJECTION_PATTERNS_JA = [
    r"(?:これまで|以前|すべて|直前)の(?:指示|命令|プロンプト|ルール)(?:を)?(?:無視|無効|破棄)",
    r"(?:あなたは|君は)(?:もう|もはや)(?:アシスタント|AI|エージェント)?(?:ではない|ではなく)",
    r"(?:システムプロンプト|プロンプト|設定|指示)(?:を)?(?:表示|見せて|教えて|明かして|出力)",
    r"(?:今から|これから)(?:あなた|君)は(?:人間|ユーザー|管理者|システム)(?:のふり|として|に)?",
    r"(?:制限|制約|ガードレール|安全対策)(?:を)?(?:無視|バイパス|回避|無効化)",
    r"(?:指示|命令)(?:を)?(?:上書き|書き換え|変更)",
    r"あなたの名前は(?:ChatGPT|Claude|GPT|AIアシスタント|助手)",
]

# 韩文注入
INJECTION_PATTERNS_KO = [
    r"(?:이전|모든|이전의)\s*(?:지시|명령|규칙|프롬프트)(?:를)?\s*(?:무시|폐기|잊어)",
    r"(?:너는|당신은)(?:더 이상|이제)?(?:어시스턴트|AI|에이전트)?(?:가|이)?\s*(?:아니다|아니야)",
    r"(?:시스템|프롬프트|설정|지시)(?:를)?\s*(?:보여줘|알려줘|표시|공개|출력)",
    r"(?:지금부터|이제부터)\s*(?:너는|당신은)\s*(?:사람|사용자|관리자|시스템)\s*(?:인 척|처럼|으로)",
    r"(?:제한|제약|안전장치|가드레일)(?:을)?\s*(?:무시|우회|무력화)",
    r"(?:지시|명령)(?:을)?\s*(?:덮어쓰기|변경|수정)",
    r"너의 이름은 (?:ChatGPT|Claude|GPT|AI 어시스턴트|조수)",
]

# 俄语注入（西里尔）
INJECTION_PATTERNS_RU = [
    r"(?i)игнорируй\s+(?:все|все|предыдущие|свои)\s+(?:инструкции|указания|правила|промпт)",
    r"(?i)забудь\s+(?:все|свои|предыдущие)\s+(?:инструкции|указания|правила)",
    r"(?i)(?:покажи|расскажи|раскрой|выведи|открой)\s+(?:свой|свои)?\s*(?:системный\s+промпт|промпт|инструкции|настройки)",
    r"(?i)ты\s+(?:больше\s+)?(?:не\s+)?(?:ассистент|бот|ии|агент)",
    r"(?i)обойди\s+(?:все\s+)?(?:ограничения|запреты|правила|защиту)",
    r"(?i)переопредели\s+(?:свои|все|предыдущие)\s+(?:инструкции|указания)",
    r"(?i)притворяйся\s+(?:человеком|пользователем|администратором|системой)",
]

# 阿拉伯语注入
INJECTION_PATTERNS_AR = [
    r"تجاهل\s+(?:جميع\s+)?(?:التعليمات|الأوامر|القواعد|البرومبت)\s+(?:السابقة|الخاصة\s+بك)",
    r"(?:انسى|انسي)\s+(?:جميع\s+)?(?:التعليمات|الأوامر|القواعد)",
    r"(?:أظهر|اكشف|أخبرني|اعرض)\s+(?:البرومبت\s+النظامي|برومبتك|تعليماتك|إعداداتك)",
    r"(?:تجاوز|التفاف\s+على)\s+(?:جميع\s+)?(?:القيود|المحظورات|الضوابط)",
    r"أنت\s+(?:لست|لم\s+تعد)\s+(?:مساعد|بوت|ذكاء\s+اصطناعي)",
    r"(?:تظاهر|ادّع)\s+أنك\s+(?:إنسان|مستخدم|مدير|نظام)",
]

# 希伯来语注入
INJECTION_PATTERNS_HE = [
    r"(?i)התעלם\s+(?:מכל\s+)?(?:ההוראות|ההנחיות|הכללים|הפרומפט)\s+(?:הקודמות|שלך)",
    r"(?i)שכח\s+(?:את\s+)?(?:כל\s+)?(?:ההוראות|ההנחיות|הכללים)",
    r"(?i)(?:הראה|חשף|ספר\s+לי|הצג)\s+(?:את\s+)?(?:הפרומפט\s+המערכתי|הפרומפט\s+שלך|ההנחיות\s+שלך)",
    r"(?i)עקוף\s+(?:את\s+)?(?:כל\s+)?(?:המגבלות|האיסורים|ההגבלות)",
    r"אתה\s+(?:לא\s+)?(?:עוד\s+)?(?:עוזר|בוט|בינה\s+מלאכותית)",
    r"(?i)התחזה\s+ל(?:אדם|משתמש|מנהל|מערכת)",
]

# 泰语注入（泰语无空格分词，词间用 \s* 而非 \s+）
INJECTION_PATTERNS_TH = [
    r"ละเลย\s*(?:คำสั่ง|คำแนะนำ|กฎ|พรอมต์)\s*(?:ก่อนหน้า|ทั้งหมด)",
    r"ลืม\s*(?:คำสั่ง|คำแนะนำ|กฎ)\s*(?:ทั้งหมด|ก่อนหน้า)",
    r"(?:แสดง|เปิดเผย|บอก)\s*(?:พรอมต์ระบบ|พรอมต์ของคุณ|คำสั่งของคุณ)",
    r"(?:หลีกเลี่ยง|บายพาส)\s*(?:ข้อจำกัด|ข้อห้าม|การป้องกัน)",
    r"คุณ\s*(?:ไม่ใช่|ไม่ได้เป็น)\s*(?:ผู้ช่วย|บอท|AI)",
    r"แกล้งทำเป็น\s*(?:มนุษย์|ผู้ใช้|ผู้ดูแลระบบ|ระบบ)",
]

# 希腊语注入
INJECTION_PATTERNS_EL = [
    r"(?i)αγνόησε\s+(?:όλες\s+)?(?:τις\s+)?(?:οδηγίες|εντολές|κανόνες|προτροπές)\s+(?:προηγούμενες|σου)",
    r"(?i)ξέχασε\s+(?:όλες\s+)?(?:τις\s+)?(?:οδηγίες|εντολές|κανόνες)",
    r"(?i)(?:δείξε|αποκάλυψε|πες\s+μου)\s+(?:το\s+)?(?:σύστημα\s+προτροπής|προτροπή\s+σου|οδηγίες\s+σου)",
    r"(?i)παράκαμψε\s+(?:όλους\s+)?(?:τους\s+)?(?:περιορισμούς|κανόνες|απαγορεύσεις)",
    r"δεν\s+είσαι\s+(?:πια\s+)?(?:βοηθός|bot|τεχνητή\s+νοημοσύνη)",
    r"υποδύσου\s+(?:τον\s+)?(?:άνθρωπο|χρήστη|διαχειριστή|σύστημα)",
]

# 按语系分类的注入模式表（lang_detect 主语言标签 → 模式列表）
LANG_INJECTION_PATTERNS: dict[str, list[str]] = {
    "zh": INJECTION_PATTERNS_ZH,
    "ja": INJECTION_PATTERNS_JA,
    "ko": INJECTION_PATTERNS_KO,
    "cyrillic": INJECTION_PATTERNS_RU,
    "arabic": INJECTION_PATTERNS_AR,
    "hebrew": INJECTION_PATTERNS_HE,
    "thai": INJECTION_PATTERNS_TH,
    "greek": INJECTION_PATTERNS_EL,
}

# 通用检测模式（任何语系都查）
INJECTION_PATTERNS = INJECTION_PATTERNS_EN

EXFILTRATION_PATTERNS = [
    r"(?i)(?:show|tell|give|reveal|share|provide)\s+(?:me\s+)?(?:your|the)\s+(?:api|auth|access|secret|key|token)",
    r"(?i)(?:send|email|give)\s+me\s+(?:your|the)\s+(?:credentials|api\s+key|access\s+token)",
    r"(?i)environment\s+variables?",
    r"(?i)database\s+(?:connection|credentials|password)",
    r"(?i)(?:\.env|config\.json|settings\.py|secrets\.json)",
    r"(?i)/etc/passwd",
    r"(?i)/etc/shadow",
    r"(?i)operator\s+(?:key|token|password|credentials)",
    r"(?i)admin\s+(?:key|token|password|credentials|access)",
    r"(?i)private[\s_-]*key",
    r"(?i)signing[\s_-]*key",
    # 中文
    r"(?:把|将|给|发送|告诉我)(?:你的|您的)?(?:API|密钥|密码|令牌|token|key|凭据|账号密码)",
    r"(?:读取|导出|查询|列出)(?:你的|您的)?(?:环境变量|配置文件|数据库|凭据)",
    # 日文
    r"(?:あなたの|君の|お前の)\s*(?:API|キー|トークン|パスワード|認証情報|パスワード|秘密鍵)",
    r"(?:環境変数|設定ファイル|データベース|認証情報)\s*(?:を)?\s*(?:見せて|教えて|読み出して|取得|送信)",
    # 韩文
    r"(?:너의|당신의)\s*(?:API|키|토큰|비밀번호|자격증명|인증정보)",
    r"(?:환경변수|설정파일|데이터베이스|자격증명)\s*(?:을)?\s*(?:보여줘|알려줘|가져와|전송)",
    # 俄语
    r"(?i)(?:покажи|расскажи|дай|отправь|пришли)\s+(?:мне\s+)?(?:свой|свои)?\s*(?:api\s+ключ|ключ|токен|пароль|учётные\s+данные|секрет)",
    r"(?i)(?:переменные\s+окружения|файлы\s+конфигурации|база\s+данных|учётные\s+данные)",
    # 阿拉伯语
    r"(?:مفتاح|كلمة\s+المرور|رمز|بيانات\s+الاعتماد)\s+(?:الخاص\s+بك|الخاصة\s+بك)",
    r"(?:متغيرات\s+البيئة|ملفات\s+الإعداد|قاعدة\s+البيانات|بيانات\s+الاعتماد)",
]

IMPERSONATION_PATTERNS = [
    r"(?i)(?:this\s+is|i\s+am)\s+(?:the\s+)?(?:system|admin|operator)",
    r"(?i)message\s+from\s+(?:the\s+)?(?:system|admin|operator|platform)",
    r"(?i)authorized\s+by\s+(?:the\s+)?(?:system|admin|operator)",
    r"(?i)System\.execute\s*\(",
    r"(?i)rm\s+-rf\s+/",
    # 中文
    r"(?:这是|我是)\s*(?:系统|管理员|平台|官方)",
    r"(?:来自|以下消息来自)\s*(?:系统|管理员|平台|官方)",
    # 日文
    r"(?:これは|こちらは)\s*(?:システム|管理者|プラットフォーム|公式)",
    r"(?:システム|管理者|プラットフォーム)\s*(?:からの|による)\s*(?:メッセージ|通知)",
    # 韩文
    r"(?:이것은|이\s+메시지는)\s*(?:시스템|관리자|플랫폼|공식)",
    r"(?:시스템|관리자|플랫폼)\s*(?:으로부터의|의)\s*(?:메시지|알림)",
    # 俄语
    r"(?i)(?:это\s+|это\s+сообщение\s+от\s+)(?:системы|администратора|платформы|официально)",
    r"(?i)(?:от\s+имени\s+)?(?:системы|администратора|оператора)",
    # 阿拉伯语
    r"(?:هذه\s+رسالة\s+من|هذا\s+من)\s*(?:النظام|المشرف|المنصة|الرسمي)",
    r"(?:مصرح\s+من\s+قبل|باسم)\s*(?:النظام|المشرف|المنصة)",
]

XSS_PATTERNS = [
    r"<script[\s>]",
    r"(?i)javascript\s*:",
    r"(?i)onerror\s*=",
    r"(?i)onload\s*=",
    r"(?i)document\.cookie",
    r"(?i)\.innerHTML\s*=",
    r"(?i)eval\s*\(",
    r"(?i)window\.location",
]

# 检测但不过度脱敏的文件路径类模式
_NO_REDACT_PATTERNS: frozenset = frozenset({
    r"(?i)/etc/passwd",
    r"(?i)/etc/shadow",
    r"(?i)(?:\.env|config\.json|settings\.py|secrets\.json)",
})

_XSS_REDACTION_PATTERNS: list[tuple[str, str]] = [
    (r"<script[^>]*>[\s\S]*?</script>", "[REDACTED]"),
    (r"(?i)javascript\s*:[^\s\"']*", "[REDACTED]"),
    (r"(?i)on(?:error|load|click)\s*=[^\s>]*", "[REDACTED]"),
    (r"(?i)document\.cookie", "[REDACTED]"),
    (r"(?i)\.innerHTML\s*=[^\n;]*", "[REDACTED]"),
    (r"(?i)eval\s*\([^)]*\)", "[REDACTED]"),
    (r"(?i)window\.location\s*=[^\n;]*", "[REDACTED]"),
]

# 语义意图词表（中文 + 英文）
INTENT_VOCABULARIES = {
    "override": {"ignore", "disregard", "bypass", "override", "cancel", "replace",
                 "forget", "忽略", "无视", "绕过", "忘记", "覆盖", "取消",
                 "無視", "破棄", "上書き", "バイパス", "忘れ",
                 "무시", "폐기", "덮어쓰기", "우회", "잊어",
                 "игнорируй", "забудь", "обойди", "переопредели"},
    "authority": {"admin", "system", "operator", "developer", "root", "sudo",
                  "administrator", "superuser", "系统", "管理员", "开发者", "平台",
                  "システム", "管理者", "開発者", "プラットフォーム",
                  "시스템", "관리자", "개발자", "플랫폼",
                  "система", "администратор", "оператор"},
    "extraction": {"show", "tell", "give", "reveal", "dump", "export", "provide",
                   "share", "display", "显示", "告诉", "提供", "输出", "导出", "泄露",
                   "表示", "教えて", "明かして", "出力", "提供",
                   "보여줘", "알려줘", "공개", "출력",
                   "покажи", "расскажи", "раскрой", "выведи"},
    "secrets": {"key", "token", "password", "secret", "credential", "private",
                "confidential", "signing", "密钥", "密码", "令牌", "凭据", "token",
                "キー", "トークン", "パスワード", "認証情報", "秘密鍵",
                "키", "토큰", "비밀번호", "자격증명", "인증정보",
                "ключ", "токен", "пароль", "учётные"},
}

INVISIBLE_CHARS = "\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
RTL_CHARS = "\u202a\u202b\u202c\u202d\u202e"

CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "с": "c", "р": "p",
    "х": "x", "у": "y", "А": "A", "Е": "E", "О": "O",
    "С": "C", "Р": "P", "Х": "X", "У": "Y", "Т": "T",
}

WEIGHTS = {
    "prompt_injection": 1.0,
    "instruction_override": 1.0,
    "data_exfiltration": 0.9,
    "impersonation": 0.8,
    "payload_smuggling": 0.9,
    "xss_injection": 0.9,
    "recursive_injection": 1.0,
}


# ── 清洗引擎 ──────────────────────────────────────────────────────────────

class ContentScrubber:
    """多阶段内容清洗：语言感知 → 编码归一化 → 模式检测 → 语义分析 → 风险评分 → 脱敏。

    语言感知：先用 lang_detect.detect_language 判定内容主语言，
    按语系合并注入模式（英语通用 + 该语系专有），避免对全语系
    全量扫描（低误报、高性能），与 argo 多语言路由对齐。
    """

    def scrub(self, content: str) -> ScrubResult:
        if not content:
            return ScrubResult(clean=True, content=content)

        threats: list[ThreatDetection] = []

        # 阶段 0：语言感知（复用 lang_detect 原生能力）
        lang = self._detect_content_lang(content)

        # 阶段 1：编码归一化（语言感知，避免西里尔/希腊正文误判）
        normalized, encoding_threats, layers = self._normalize_encoding(content, lang)
        threats.extend(encoding_threats)

        # 阶段 2：模式检测（按语系加载注入模式）
        threats.extend(self._detect_injections(normalized, lang))
        threats.extend(self._detect_exfiltration(normalized))
        threats.extend(self._detect_impersonation(normalized))
        threats.extend(self._detect_xss(normalized))

        # 阶段 3：语义意图分析
        threats.extend(self._semantic_analysis(normalized))

        # 阶段 4：风险评分
        risk_score = self._calculate_risk(threats)

        # 阶段 5：内容清洗（检测与脱敏共用模式表）
        cleaned, redactions = self._clean_content(normalized, threats, risk_score)

        return ScrubResult(
            clean=(risk_score < 0.5),
            content=cleaned,
            threats=threats,
            risk_score=risk_score,
            redactions=redactions,
            encoding_layers_stripped=layers,
            content_lang=lang,
        )

    def _normalize_encoding(self, text: str, lang: str = "other") -> tuple[str, list[ThreatDetection], int]:
        """检测并剥离编码伪装。"""
        threats: list[ThreatDetection] = []
        current = text
        layers = 0

        # Unicode 同形字归一化——仅对拉丁语系内容启用
        # （西里尔/希腊正文本身就是这些字母，误判为同形字攻击会产生大量误报；
        #   同形字攻击是「西里尔伪拉丁」混入英文内容，只在 en/latin 下有意义）
        if lang in ("en", "latin", "zh", "ja", "ko", "mixed"):
            normalized = unicodedata.normalize("NFKD", current)
            cyrillic_found = any(c in current for c in CONFUSABLES)
            if cyrillic_found:
                for cyrillic, latin in CONFUSABLES.items():
                    normalized = normalized.replace(cyrillic, latin)
                if normalized != current:
                    threats.append(ThreatDetection(
                        threat_type="payload_smuggling", confidence=0.7,
                        evidence="Unicode 同形字已归一化", location="unicode"))
                    current = normalized
                    layers += 1

        # 零宽字符剥离
        stripped = "".join(c for c in current if c not in INVISIBLE_CHARS)
        if stripped != current:
            removed = len(current) - len(stripped)
            threats.append(ThreatDetection(
                threat_type="payload_smuggling", confidence=0.8,
                evidence=f"剥离 {removed} 个零宽/不可见字符", location="zero_width"))
            current = stripped
            layers += 1

        # RTL/LTR 覆盖符剥离
        stripped = "".join(c for c in current if c not in RTL_CHARS)
        if stripped != current:
            threats.append(ThreatDetection(
                threat_type="payload_smuggling", confidence=0.8,
                evidence="剥离 RTL/LTR 覆盖符", location="rtl_override"))
            current = stripped
            layers += 1

        # base64 片段检测
        for frag in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", current):
            try:
                decoded = base64.b64decode(frag).decode("utf-8")
                if len(decoded) >= 10 and _looks_suspicious(decoded):
                    threats.append(ThreatDetection(
                        threat_type="payload_smuggling", confidence=0.8,
                        evidence=f"base64 片段解码为可疑内容: {decoded[:60]}",
                        location="base64_fragment"))
                    layers += 1
            except Exception:
                pass

        # URL 编码
        if "%" in current:
            try:
                decoded = urllib.parse.unquote(current)
                if decoded != current:
                    threats.append(ThreatDetection(
                        threat_type="payload_smuggling", confidence=0.5,
                        evidence="URL 编码内容已归一化", location="url_encoding"))
                    current = decoded
                    layers += 1
            except Exception:
                pass

        # 递归编码标记
        if layers > 2:
            threats.append(ThreatDetection(
                threat_type="recursive_injection", confidence=1.0,
                evidence=f"{layers} 层编码——疑似对抗性内容", location="nested_encoding"))

        return current, threats, layers

    def _detect_content_lang(self, content: str) -> str:
        """复用 lang_detect.detect_language 判定内容主语言（原生能力，不重复造轮子）。

        返回 zh / en / ja / ko / latin / cyrillic / thai / arabic /
        hebrew / greek / devanagari / mixed / other。
        """
        try:
            from lang_detect import detect_language
            return detect_language(content)
        except Exception:
            return "other"

    @staticmethod
    @lru_cache(maxsize=16)
    def _lang_patterns(lang: str) -> tuple[str, ...]:
        """按语系合并注入模式：英语通用 + 该语系专有。

        lru_cache：安全引擎高频调用，模式表按语系缓存，避免每次重建列表。
        """
        patterns = list(INJECTION_PATTERNS_EN)
        # 中英混合：中文模式也查（lang_detect 对纯汉字判 zh，混合含中文）
        if lang == "zh" or lang == "mixed":
            patterns.extend(INJECTION_PATTERNS_ZH)
        for key, plist in LANG_INJECTION_PATTERNS.items():
            if lang == key:
                patterns.extend(plist)
        # en/latin 落到英文通用（已有）
        return tuple(patterns)

    def _detect_injections(self, text: str, lang: str = "other") -> list[ThreatDetection]:
        patterns = self._lang_patterns(lang)
        return self._match_patterns(text, patterns, "prompt_injection", 0.9)

    def _detect_exfiltration(self, text: str) -> list[ThreatDetection]:
        return self._match_patterns(text, EXFILTRATION_PATTERNS, "data_exfiltration", 0.85)

    def _detect_impersonation(self, text: str) -> list[ThreatDetection]:
        return self._match_patterns(text, IMPERSONATION_PATTERNS, "impersonation", 0.8)

    def _detect_xss(self, text: str) -> list[ThreatDetection]:
        return self._match_patterns(text, XSS_PATTERNS, "xss_injection", 0.9)

    @staticmethod
    def _match_patterns(text: str, patterns: list[str],
                        threat_type: str, confidence: float) -> list[ThreatDetection]:
        threats = []
        for pattern in patterns:
            try:
                if re.search(pattern, text):
                    threats.append(ThreatDetection(
                        threat_type=threat_type, confidence=confidence,
                        evidence=f"命中模式: {pattern[:60]}", location="content"))
            except re.error:
                pass
        return threats

    def _semantic_analysis(self, text: str) -> list[ThreatDetection]:
        """词表聚类意图分析。

        用「命中词数」而非「比例」判断：多语种词表共享同一语义类别，
        按比例会被词表膨胀稀释（如 3 个英文命中词在 27 词的日英混合表里
        只有 0.11）。命中 ≥2 个同类词即说明意图明确，跨语种稳健。
        """
        threats: list[ThreatDetection] = []
        words = set(re.findall(r"\w+", text.lower()))

        intent_hits = {}
        for category, vocab in INTENT_VOCABULARIES.items():
            overlap = words & vocab
            if overlap:
                intent_hits[category] = overlap

        # authority × override = 经典越狱
        if len(intent_hits.get("authority", set())) >= 2 and \
                len(intent_hits.get("override", set())) >= 2:
            threats.append(ThreatDetection(
                threat_type="instruction_override", confidence=0.85,
                evidence=(f"权威×覆盖意图 "
                          f"(authority={sorted(intent_hits['authority'])[:3]}, "
                          f"override={sorted(intent_hits['override'])[:3]})"),
                location="semantic"))

        # extraction × secrets = 数据外泄
        if len(intent_hits.get("extraction", set())) >= 2 and \
                len(intent_hits.get("secrets", set())) >= 1:
            threats.append(ThreatDetection(
                threat_type="data_exfiltration", confidence=0.9,
                evidence=(f"提取×秘密意图 "
                          f"(extract={sorted(intent_hits['extraction'])[:3]}, "
                          f"secrets={sorted(intent_hits['secrets'])[:3]})"),
                location="semantic"))

        # 3+ 意图类别有命中 = 复杂攻击（用命中类别数，跨语种稳健）
        strong = {k for k, hits in intent_hits.items() if len(hits) >= 2}
        if len(strong) >= 3:
            threats.append(ThreatDetection(
                threat_type="prompt_injection", confidence=0.7,
                evidence=f"多意图攻击: {sorted(strong)}", location="semantic"))

        return threats

    def _calculate_risk(self, threats: list[ThreatDetection]) -> float:
        if not threats:
            return 0.0
        total = sum(t.confidence * WEIGHTS.get(t.threat_type, 0.5) for t in threats)
        max_possible = len(threats) * max(WEIGHTS.values())
        risk = min(total / max_possible, 1.0) if max_possible > 0 else 0.0
        # 多威胁类型加成
        unique_types = len(set(t.threat_type for t in threats))
        if unique_types > 2:
            risk = min(risk * 1.3, 1.0)
        return round(risk, 3)

    def _clean_content(self, text: str, threats: list[ThreatDetection],
                       risk_score: float) -> tuple[str, int]:
        """脱敏：检测与脱敏共用模式表（不漂移）。高风险先脱敏后截断。"""
        if not threats:
            return text, 0

        cleaned = text
        redactions = 0

        # 与检测共用同一语系模式表（_lang_patterns），避免中文/日文等
        # 注入「被标记却不脱敏」的漂移
        lang = getattr(self, "_detect_content_lang", lambda t: "other")(text)
        for pattern in (list(self._lang_patterns(lang)) + EXFILTRATION_PATTERNS +
                        IMPERSONATION_PATTERNS):
            if pattern in _NO_REDACT_PATTERNS:
                continue
            try:
                cleaned, count = re.subn(pattern, "[REDACTED]", cleaned)
                redactions += count
            except re.error:
                pass

        for pattern, replacement in _XSS_REDACTION_PATTERNS:
            try:
                cleaned, count = re.subn(pattern, replacement, cleaned)
                redactions += count
            except re.error:
                pass

        # 高风险：先脱敏后截断，并加警告头
        if risk_score > 0.8:
            header = (
                f"[内容风险标记：{len(threats)} 处威胁 (risk={risk_score:.2f})，"
                f"按潜在对抗性内容处理]\n\n"
            )
            cleaned = header + cleaned[:3000]

        return cleaned, redactions


def _looks_suspicious(decoded: str) -> bool:
    """粗判解码文本是否含指令性内容（避免误伤普通 base64 图片/二进制）。"""
    suspicious_kw = ("ignore", "instruction", "prompt", "system", "you are",
                     "忽略", "指令", "提示词", "你是", "password", "api key",
                     "token", "密钥", "密码")
    low = decoded.lower()
    return any(kw in low for kw in suspicious_kw)


_scrubber: Any = None


def get_scrubber() -> ContentScrubber:
    global _scrubber
    if _scrubber is None:
        _scrubber = ContentScrubber()
    return _scrubber


def scrub_content(content: str) -> ScrubResult:
    """清洗入口：返回 ScrubResult。"""
    return get_scrubber().scrub(content)


def scrub_to_dict(content: str) -> dict:
    """清洗入口（dict 形态，供 fetch 层直接合并）。"""
    r = scrub_content(content)
    return {
        "content_clean": r.clean,
        "risk_score": r.risk_score,
        "threat_count": len(r.threats),
        "threat_types": sorted({t.threat_type for t in r.threats}),
        "redactions": r.redactions,
        "encoding_layers_stripped": r.encoding_layers_stripped,
        "content_lang": r.content_lang,
    }


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="Argo 内容安全引擎（注入检测 + 清洗）")
    p.add_argument("text", nargs="?", default=None, help="要检测的文本")
    p.add_argument("--stdin", action="store_true", help="从 stdin 读取")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    text = args.text
    if args.stdin:
        text = sys.stdin.read()
    if text is None:
        p.error("需要文本参数或 --stdin")

    result = scrub_content(text)
    if args.json:
        out = {
            "clean": result.clean,
            "risk_score": result.risk_score,
            "redactions": result.redactions,
            "encoding_layers_stripped": result.encoding_layers_stripped,
            "threats": [
                {"type": t.threat_type, "confidence": t.confidence,
                 "evidence": t.evidence, "location": t.location}
                for t in result.threats
            ],
            "content": result.content,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"clean={result.clean} risk={result.risk_score:.3f} "
              f"redactions={result.redactions} threats={len(result.threats)}")
        for t in result.threats:
            print(f"  [{t.threat_type}] conf={t.confidence:.2f} {t.evidence}")
        if result.threats:
            print(f"\n清洗后内容片段：\n{result.content[:300]}")
