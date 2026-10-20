"""Entry limits use the SAME saved, selected-account net PnL as the report.
No balance-debit estimate, total stake, or Martingale counter is a profit measure.
Existing completed statistics are deliberately NOT reset on enable or on Start.
"""
from decimal import Decimal
import math

RISK_MODES = ('TRADITIONAL', 'LIMITED')


def risk_defaults():
    return {'mode': 'TRADITIONAL', 'profit_limit': 50.0, 'loss_limit': 50.0}


def validate_risk(risk):
    if not isinstance(risk, dict) or risk.get('mode') not in RISK_MODES:
        raise ValueError('وضع الحدود غير صالح')
    for key in ('profit_limit', 'loss_limit'):
        value = risk.get(key)
        if isinstance(value, bool):
            raise ValueError('الحد يجب أن يكون مبلغاً موجباً')
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise ValueError('الحد يجب أن يكون مبلغاً موجباً') from None
        if not math.isfinite(number) or not .01 <= number <= 1000000:
            raise ValueError('الحد يجب أن يكون بين 0.01 و1000000 دولار')
    return risk


def risk_state(doc):
    risk = {**risk_defaults(), **doc.get('risk', {})}
    validate_risk(risk)
    mode = doc['account_mode']
    stats = doc['account_stats'][mode]
    net = float(stats.get('net_pnl', 0))
    if not math.isfinite(net):
        raise ValueError('صافي الحساب غير صالح؛ راجع الإحصائيات قبل التداول')
    # Repository delta accounting is rounded to six decimal places.
    pnl = Decimal(str(round(net, 6)))
    reason = None
    if risk['mode'] == 'LIMITED':
        if pnl >= Decimal(str(risk['profit_limit'])):
            reason = 'PROFIT_LIMIT'
        elif pnl <= -Decimal(str(risk['loss_limit'])):
            reason = 'LOSS_LIMIT'
    return dict(risk, account_mode=mode, net_pnl=round(net, 6), reason=reason,
                unconfirmed=int(stats.get('unconfirmed', 0)))


def limit_stop_text(doc):
    s = risk_state(doc)
    label = 'حد الربح' if s['reason'] == 'PROFIT_LIMIT' else 'حد الخسارة'
    return (f"🛑 توقف تلقائي: بلوغ {label}\nالحساب: {s['account_mode']}\n"
            f"صافي الربح/الخسارة: ${s['net_pnl']:+.2f}\n"
            f"حد الربح: ${s['profit_limit']:g} | حد الخسارة: ${s['loss_limit']:g}\n"
            'لن يُرسل أمر جديد، ولا يتجاوز Start هذا الحد. لتبدأ فترة جديدة صفّر السجل، '
            'أو عدّل الحدود أو اختر الوضع التقليدي ثم اضغط Start. '
            'الصفقات المفتوحة لا يمكن إلغاؤها من هنا.')


def risk_summary(doc):
    s = risk_state(doc)
    name = 'تقليدي — دون حدود' if s['mode'] == 'TRADITIONAL' else 'بحدود مفعّلة'
    text = (f"🛡 التشغيل: {name}\n"
            f"حد الربح: ${s['profit_limit']:g} | حد الخسارة: ${s['loss_limit']:g}\n"
            f"صافي الحساب المعتمد للحدود: ${s['net_pnl']:+.2f}\n"
            'الاحتساب من سجل هذا الحساب منذ آخر تصفير، وليس من مبالغ الدخول.\n')
    if s['reason']:
        text += '🛑 الحد مُتحقق — الدخول ممنوع في وضع الحدود.\n'
    if s['unconfirmed']:
        text += '⚠️ توجد نتائج غير مؤكدة لا تدخل في الصافي؛ الحدود ليست ضماناً لسقف الخسارة الفعلية.\n'
    return text
