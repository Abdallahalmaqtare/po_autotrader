"""Static allow-listed diagnostics. Never echo exception text, frames or credentials."""
from dataclasses import dataclass
from pathlib import Path
import traceback

MESSAGES = {
    'META_SYNC_FAILED': ('لم تكتمل مزامنة الزوج والمدة مع مصدر الإشارات', 'لن يُرسل أمر قبل تطابق الزوج والمدة. راجع محادثة المصدر وزر Trade/OTC/Back وتوفر المدة.'),
    'TG_SESSION_INVALID': ('جلسة Telethon غير صالحة', 'أنشئ TELEGRAM_STRING_SESSION بواسطة session_tool.py؛ لا تستخدم Bot Token أو SSID أو اسم ملف الجلسة.'),
    'TG_SESSION_ID_MISMATCH': ('جلسة الإشارات تخص معرّف تيليجرام آخر','اربط جلسة الحساب الموافق لمعرّفك المعتمد؛ لا تستخدم جلسة مستخدم آخر.'),
    'TG_SESSION_UNAUTHORIZED': ('جلسة تيليجرام غير مخوّلة أو أُلغيت', 'أعد تسجيل الدخول محلياً عبر session_tool.py --login ثم حدّث TELEGRAM_STRING_SESSION وأعد النشر.'),
    'TG_CONNECT_FAILED': ('تعذّر اتصال حساب تيليجرام', 'تحقق من الشبكة وصلاحية جلسة Telethon ثم أعد المحاولة.'),
    'TG_API_INVALID': ('بيانات API لحساب تيليجرام مرفوضة', 'راجع TELEGRAM_API_ID وTELEGRAM_API_HASH في Render؛ توكن لوحة التحكم ليس بديلاً عنهما.'),
    'SIGNAL_NAME_INVALID': ('صيغة اسم بوت الإشارات غير صالحة', 'ضع SIGNAL_BOT_USERNAME كاسم @username أو رابط t.me مباشر، ثم أعد النشر.'),
    'SIGNAL_BOT_UNRESOLVED': ('تعذّر العثور على بوت الإشارات', 'راجع SIGNAL_BOT_USERNAME في Render وافتحه من حساب تيليجرام المستخدم في StringSession واضغط Start يدوياً.'),
    'SIGNAL_TARGET_NOT_BOT': ('مصدر الإشارات ليس حساب بوت', 'راجع اسم المصدر؛ لا تستخدم اسم قناة أو مستخدم أو بوت التحكم بالخطأ.'),
    'SIGNAL_BUTTON_FAILED': ('تعذّر الضغط على زر الإشارة', 'افتح محادثة مصدر الإشارات من نفس حساب الجلسة وتأكد من وجود زر New signal صالح.'),
    'SIGNAL_READ_FAILED': ('تعذّر قراءة إشارة تيليجرام', 'راجع اتصال حساب الجلسة ومحادثة بوت الإشارات.'),
    'PO_CREDENTIALS_INVALID': ('بيانات حساب المنصة غير صالحة', 'حدّث بيانات الحساب المختار من Accounts؛ MongoDB تحتفظ بالقيم السابقة حتى إن عدّلت Render. لا تستخدم session=... أو uid=0.'),
    'PO_CONNECT_FAILED': ('تعذّر اتصال منصة التداول', 'راجع SSID والرابط للحساب المختار وصلاحيتهما؛ لا تشاركهما في السجلات.'),
    'PO_AUTH_REJECTED': ('المنصة رفضت المصادقة', 'التقط إطار auth جديداً من حسابك وحدّثه عبر Accounts دون تغيير الحساب بالصدفة.'),
    'PO_ACCOUNT_MISMATCH': ('بيانات الجلسة لا تطابق نوع الحساب المختار', 'الـ SSID المخزّن يخص الحساب الآخر (ديمو/حقيقي). من Accounts: بدّل نوع الحساب ليطابق الجلسة، أو حدّث بيانات الحساب المختار بجلسة ملتقطة في نفس الوضع.'),
    'PO_WS_HTTP_401': ('رفض WebSocket بيانات المصادقة', 'حدّث جلسة الحساب ورابط wss من المتصفح.'),
    'PO_WS_HTTP_403': ('رفض خادم WebSocket الاتصال', 'تحقق من رابط الحساب والجلسة وسياسة شبكة الاستضافة؛ لا نحاول تجاوز حظر الخادم.'),
    'PO_WS_HTTP_404': ('مسار WebSocket غير صحيح', 'حدّث رابط wss بالكامل مع socket.io وEIO وtransport.'),
    'PO_WS_HTTP_429': ('طلبات اتصال كثيرة', 'انتظر ثم أعد المحاولة دون تكرار الضغط.'),
    'PO_WS_HTTP_502': ('بوابة WebSocket غير متاحة', 'أعد المحاولة أو حدّث رابط الحساب من المتصفح.'),
    'PO_WS_HTTP_503': ('خدمة WebSocket غير متاحة مؤقتاً', 'أعد المحاولة بعد قليل.'),
    'PO_WS_TIMEOUT': ('انتهت مهلة فتح WebSocket', 'تحقق من رابط الحساب واتصال الاستضافة.'),
    'PO_WS_NETWORK': ('انقطع اتصال شبكة المنصة', 'تحقق من عنوان wss وشبكة الاستضافة؛ لم يُعد إرسال أمر تداول.'),
    'PO_WS_TLS': ('فشل التحقق من شهادة الاتصال', 'استخدم رابط المنصة الأصلي؛ لا نعطل التحقق من الشهادة.'),
    'PO_ENGINE_OPEN_TIMEOUT': ('لم تصل بداية Engine.IO', 'تحقق من مسار ورابط WebSocket الخاص بالحساب.'),
    'PO_NAMESPACE_TIMEOUT': ('لم يتأكد اتصال Socket.IO', 'حدّث الرابط مع EIO=4 وtransport=websocket.'),
    'PO_AUTH_NO_ACK': ('فُتح WebSocket ولم تتأكد مصادقة الحساب', 'حدّث SSID للحساب نفسه؛ فتح الشبكة لا يساوي نجاح تسجيل الدخول.'),
    'PO_WS_PROTOCOL': ('صيغة استجابة WebSocket غير مدعومة', 'تحقق من أن الرابط يعود إلى Socket.IO الخاص بالمنصة.'),
    'PO_WS_CLOSED': ('أُغلق اتصال WebSocket', 'تُعاد المحاولة بين الصفقات؛ لا يُكرر أمر مجهول النتيجة.'),
    'PO_BALANCE_MISSING': ('لم يصل رصيد الحساب', 'لا يمكن الدخول دون رصيد معروف؛ راجع اتصال المنصة والحساب المختار.'),
    'PO_DISCONNECTED': ('انقطع اتصال المنصة قبل إرسال الأمر', 'أعد الاتصال؛ لا يُعاد إرسال إشارة قديمة تلقائياً.'),
    'INSUFFICIENT_BALANCE': ('الرصيد لا يكفي مبلغ الصفقة', 'راجع الرصيد والمبلغ ومستوى التدبيل قبل إعادة البدء.'),
    'ORDER_UNCERTAIN': ('إرسال أو نتيجة الأمر غير محسومة', 'راجع المنصة أولاً؛ لا تضغط Start لتكرار الأمر. استخدم مراجعة الأمر المعلّق بعد التأكد.'),
    'DATABASE_FAILED': ('تعذّرت عملية قاعدة البيانات', 'راجع اتصال MongoDB وملكية lease؛ قد يبقى أمر للمراجعة إذا انقطع الحفظ بعد الإرسال.'),
    'WORKER_FAILED': ('تعذّر إكمال مرحلة التداول', 'أرسل كود التشخيص والمرحلة وموقع الملف فقط؛ لا ترسل توكنات أو جلسات أو رابط MongoDB.'),
}


class SafeIssue(Exception):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'WORKER_FAILED'
        super().__init__(self.code)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    stage: str
    error_type: str
    location: str

    def public_text(self):
        title, hint = MESSAGES[self.code]
        return f'{self.code}\nالمرحلة: {self.stage}\n{title}\nالإجراء: {hint}\nالموقع: {self.location}'

    def log_line(self):
        return f'[WORKER] paused; code={self.code}; stage={self.stage}; type={self.error_type}; at={self.location}'


def diagnose_exception(exc, stage):
    if isinstance(exc, SafeIssue):
        code = exc.code
    elif type(exc).__name__ in ('ApiIdInvalidError', 'ApiIdPublishedFloodError'):
        code = 'TG_API_INVALID'
    elif type(exc).__name__ in ('AuthKeyUnregisteredError', 'AuthKeyDuplicatedError', 'SessionRevokedError', 'SessionExpiredError'):
        code = 'TG_SESSION_UNAUTHORIZED'
    elif stage == 'telegram_session_parse':
        code = 'TG_SESSION_INVALID'
    elif stage == 'signal_bot_resolve':
        code = 'SIGNAL_BOT_UNRESOLVED'
    elif stage.startswith('telegram_'):
        code = 'TG_CONNECT_FAILED'
    elif stage == 'pocket_credentials':
        code = 'PO_CREDENTIALS_INVALID'
    elif stage.startswith('pocket_'):
        code = 'PO_CONNECT_FAILED'
    elif stage == 'signal_button':
        code = 'SIGNAL_BUTTON_FAILED'
    elif stage == 'signal_wait':
        code = 'SIGNAL_READ_FAILED'
    elif stage in ('order_send', 'order_result'):
        code = 'ORDER_UNCERTAIN'
    elif stage in ('order_reserve', 'result_save'):
        code = 'DATABASE_FAILED'
    else:
        code = 'WORKER_FAILED'
    # Traceback locations only, never lines of code, locals or exception messages.
    location = '-'
    if exc.__traceback__:
        frames = traceback.extract_tb(exc.__traceback__)
        project = Path(__file__).resolve().parent
        local = [f for f in frames if Path(f.filename).resolve().parent == project]
        frame = (local or frames)[-1]
        location = f'{Path(frame.filename).name}:{frame.lineno}'
    return Diagnostic(code, stage, type(exc).__name__, location)
